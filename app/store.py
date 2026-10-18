"""In-memory data store: POIs + embedding index + landmarks. Owner: CORE lane (task T22).

Index format (produced by the DATA lane, pipeline/build_index.py):
  data/index/embeddings.npy  float32 (N, D), L2-normalised, row i <-> ids[i]
  data/index/ids.json        list[str] of POI ids, same order
  data/index/meta.json       {"model": str, "dim": int, "count": int, "created_at": str, ...}
If the index is missing, incompatible, or its document fingerprint cannot be verified
-> embeddings=None
(retrieval falls back to lexical scoring) and a warning is logged.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from pydantic import ValidationError

from app.config import Settings
from app.schemas import POI, Landmark
from pipeline.build_index import docs_hash

log = logging.getLogger(__name__)


@dataclass
class Store:
    pois: list[POI]
    by_id: dict[str, POI]
    landmarks: dict[str, Landmark]
    embeddings: np.ndarray | None = None  # (N, D) aligned with `pois`
    embed_model: str | None = None
    snapshot_date: str | None = None  # max(poi.fetched_at)
    warnings: list[str] = field(default_factory=list)


def _read_pois(path: Path) -> tuple[list[POI], int, str | None]:
    """Valid, de-duplicated POIs, the number of skipped rows and a file-level error, if any."""
    if not path.is_file():
        log.warning("POI file not found: %s", path)
        return [], 0, f"нет файла POI: {path.name}"
    pois: list[POI] = []
    seen: set[str] = set()
    skipped = 0
    try:
        with path.open(encoding="utf-8-sig") as fh:  # -sig: tolerate a BOM from Windows tools
            for lineno, line in enumerate(fh, start=1):
                if not line.strip():
                    continue
                try:
                    poi = POI.model_validate_json(line)
                except ValidationError:
                    skipped += 1
                    log.warning("skip invalid POI row %s:%d", path.name, lineno)
                    continue
                if poi.id in seen:
                    skipped += 1
                    log.warning("skip duplicate POI id %s at %s:%d", poi.id, path.name, lineno)
                    continue
                seen.add(poi.id)
                pois.append(poi)
    except (OSError, UnicodeError) as exc:
        log.warning("POI file unreadable: %s (%s)", path, type(exc).__name__)
        return [], skipped, f"файл POI не читается ({type(exc).__name__}): {path.name}"
    return pois, skipped, None


def load_pois(path: Path) -> list[POI]:
    """Read a JSONL file of POI (utf-8). Skip and count invalid lines; never crash on a bad row."""
    return _read_pois(path)[0]


def load_landmarks(path: Path) -> dict[str, Landmark]:
    if not path.is_file():
        log.warning("landmarks file not found: %s", path)
        return {}
    try:
        rows = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        log.warning("landmarks file is not readable JSON: %s", path)
        return {}
    landmarks: dict[str, Landmark] = {}
    for row in rows if isinstance(rows, list) else []:
        try:
            lm = Landmark.model_validate(row)
        except ValidationError:
            log.warning("skip invalid landmark in %s", path.name)
            continue
        landmarks.setdefault(lm.id, lm)
    return landmarks


def _load_embeddings(
    settings: Settings, pois: list[POI]
) -> tuple[np.ndarray | None, str | None, list[str]]:
    """Index rows re-ordered to match `pois`; (None, None, warnings) when unusable."""
    lexical = "релевантность считается лексически"
    index_dir = settings.index_dir
    paths = [index_dir / name for name in ("embeddings.npy", "ids.json", "meta.json")]
    if not all(p.exists() for p in paths):
        return None, None, [f"индекс эмбеддингов не найден, {lexical}"]
    try:
        matrix = np.load(paths[0], allow_pickle=False)
        ids = json.loads(paths[1].read_text(encoding="utf-8-sig"))
        meta = json.loads(paths[2].read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, EOFError) as exc:  # EOFError: empty / truncated .npy
        return None, None, [f"индекс эмбеддингов не читается ({type(exc).__name__}), {lexical}"]
    if (
        not isinstance(meta, dict)
        or not isinstance(ids, list)
        or not all(isinstance(x, str) for x in ids)
        or not isinstance(matrix, np.ndarray)
        or matrix.dtype.kind != "f"
    ):
        return None, None, [f"индекс эмбеддингов в неверном формате, {lexical}"]

    model, dim = meta.get("model"), meta.get("dim")
    if model != settings.embed_model or dim != settings.embed_dim:
        return None, None, [
            f"индекс построен для {model}/{dim}, а в настройках "
            f"{settings.embed_model}/{settings.embed_dim}, {lexical}"
        ]
    if matrix.ndim != 2 or matrix.shape != (len(ids), dim):
        return None, None, [f"индекс эмбеддингов повреждён (shape {matrix.shape}), {lexical}"]

    # Equal row counts alone do not prove that the vectors describe today's POIs.
    # The writer hashes each ID and the exact document text used for embedding.
    # Use saved index order so a harmless reorder of poi.jsonl still aligns by ID.
    by_id = {poi.id: poi for poi in pois}
    if (
        len(ids) != len(set(ids))
        or meta.get("count") != len(ids)
        or set(ids) != set(by_id)
    ):
        return None, None, [f"индекс эмбеддингов устарел: состав POI изменился, {lexical}"]
    fingerprint = meta.get("docs_sha256")
    if not isinstance(fingerprint, str) or not fingerprint:
        return None, None, [
            f"свежесть индекса не подтверждена: нет docs_sha256; пересоберите индекс, {lexical}"
        ]
    if fingerprint != docs_hash([by_id[poi_id] for poi_id in ids]):
        return None, None, [
            f"индекс эмбеддингов устарел: тексты POI изменились; пересоберите индекс, {lexical}"
        ]

    row_of = {poi_id: row for row, poi_id in enumerate(ids)}
    aligned = np.zeros((len(pois), dim), dtype=np.float32)
    missing = 0
    for i, poi in enumerate(pois):
        row = row_of.get(poi.id)
        if row is None or not np.isfinite(matrix[row]).all():
            missing += 1  # zero row: retrieval scores this POI lexically
        else:
            aligned[i] = matrix[row]
    if pois and missing == len(pois):
        return None, None, [f"в индексе нет ни одного POI из файла, {lexical}"]
    norms = np.linalg.norm(aligned, axis=1, keepdims=True)
    aligned = np.divide(aligned, norms, out=np.zeros_like(aligned), where=norms > 0)
    warnings = [f"{missing} POI без эмбеддинга (только лексика)"] if missing else []
    return aligned, str(model), warnings


def load_store(settings: Settings, poi_path: Path | None = None) -> Store:
    """Build the Store. Align embeddings rows to `pois` order via ids.json."""
    path = poi_path or settings.poi_path
    warnings: list[str] = []
    pois, skipped, error = _read_pois(path)
    if error:
        warnings.append(error)
    if skipped:
        warnings.append(f"пропущено строк POI с ошибками или повторами: {skipped}")

    landmarks = load_landmarks(settings.landmarks_path)
    if not landmarks:
        warnings.append("нет справочника ориентиров")

    embeddings, embed_model, index_warnings = _load_embeddings(settings, pois)
    warnings.extend(index_warnings)
    for message in warnings:
        log.warning(message)

    return Store(
        pois=pois,
        by_id={p.id: p for p in pois},
        landmarks=landmarks,
        embeddings=embeddings,
        embed_model=embed_model,
        snapshot_date=max((p.fetched_at for p in pois), default=None),
        warnings=warnings,
    )
