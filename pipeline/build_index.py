"""POI embeddings -> data/index/{embeddings.npy, ids.json, meta.json}.

    python -m pipeline.build_index

Embeddings come from Gemini and only when GEMINI_API_KEY is set (`settings.has_embeddings`):
the Anthropic API has no embeddings endpoint. Without a key (or on quota errors) the index
is skipped, the pipeline does not fail, and retrieval stays lexical; REPORT.md says so.
Vectors are cached by sha256 of the document text, so a rerun embeds only changed POIs.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import numpy as np

from app.config import get_settings
from app.hours import ASTANA_TZ
from app.schemas import POI

log = logging.getLogger(__name__)

DOC_TEMPLATE = "{name}. {category_ru}. Теги: {tags}. {description}"
TASK_TYPE = "RETRIEVAL_DOCUMENT"
BATCH = 100
RETRY_PAUSES_S: tuple[float, ...] = (5.0, 15.0, 45.0)

CATEGORY_RU: dict[str, str] = {
    "cafe": "кафе, кофейня",
    "restaurant": "ресторан",
    "fast_food": "фастфуд",
    "bar": "бар",
    "nightlife": "ночной клуб",
    "museum": "музей",
    "gallery": "галерея, арт-центр",
    "theatre": "театр",
    "cinema": "кинотеатр",
    "library": "библиотека",
    "park": "парк, сад",
    "playground": "детская площадка",
    "viewpoint": "смотровая площадка",
    "attraction": "достопримечательность",
    "monument": "памятник",
    "religious": "религиозное место",
    "mall": "торговый центр",
    "sport": "спорт",
    "entertainment": "развлечения",
    "other": "другое",
}

EmbedFn = Callable[[list[str]], list[list[float]]]


@dataclass
class IndexResult:
    status: str  # "built" | "skipped"
    reason: str
    count: int = 0


def doc_text(poi: POI) -> str:
    text = DOC_TEMPLATE.format(
        name=poi.name,
        category_ru=CATEGORY_RU.get(poi.category, poi.category),
        tags=", ".join(poi.tags) if poi.tags else "—",
        description=poi.description or "",
    )
    return text.strip()


def text_key(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def gemini_embedder() -> EmbedFn:
    from google import genai
    from google.genai import types

    settings = get_settings()
    client = genai.Client(api_key=settings.gemini_api_key)
    config = types.EmbedContentConfig(
        task_type=TASK_TYPE, output_dimensionality=settings.embed_dim
    )

    def embed(batch: list[str]) -> list[list[float]]:
        resp = client.models.embed_content(
            model=settings.embed_model, contents=batch, config=config
        )
        return [list(e.values) for e in resp.embeddings]

    return embed


def is_daily_quota(exc: Exception) -> bool:
    text = str(exc)
    return "RESOURCE_EXHAUSTED" in text and "PerDay" in text


def failure_reason(exc: Exception) -> str:
    """Short human reason for REPORT.md instead of the raw SDK error JSON."""
    text = str(exc)
    if is_daily_quota(exc):
        return (
            "дневная квота Gemini исчерпана (429 RESOURCE_EXHAUSTED, бесплатный тариф: "
            "1000 эмбеддингов в сутки); перезапустите pipeline.build_index после сброса квоты"
        )
    if "RESOURCE_EXHAUSTED" in text or "429" in text:
        return "лимит запросов Gemini (429), повторы не помогли"
    return f"эмбеддинги не получены ({type(exc).__name__}: {text[:120]})"


def with_retries(
    fn: EmbedFn,
    pauses: Sequence[float] = RETRY_PAUSES_S,
    sleep: Callable[[float], None] = time.sleep,
) -> EmbedFn:
    def wrapped(batch: list[str]) -> list[list[float]]:
        for attempt in range(len(pauses) + 1):
            try:
                return fn(batch)
            except Exception as exc:  # SDK raises its own error types; retry any of them
                if attempt == len(pauses) or is_daily_quota(exc):
                    raise  # a daily quota resets in hours: retrying in seconds only wastes time
                log.warning("embed batch failed (%s), retry in %ss", exc, pauses[attempt])
                sleep(pauses[attempt])
        raise AssertionError("unreachable")

    return wrapped


def load_cache(path: Path) -> dict[str, np.ndarray]:
    if not path.exists():
        return {}
    data = np.load(path, allow_pickle=False)
    return dict(zip(data["keys"].tolist(), data["vecs"], strict=True))


def save_cache(path: Path, cache: dict[str, np.ndarray]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    keys = list(cache)
    vecs = np.stack([cache[k] for k in keys]).astype(np.float32)
    with path.open("wb") as fh:
        np.savez(fh, keys=np.array(keys), vecs=vecs)


def build_index(
    pois: Sequence[POI],
    index_dir: Path,
    *,
    embed: EmbedFn | None = None,
    cache_path: Path | None = None,
    model: str | None = None,
    dim: int | None = None,
) -> IndexResult:
    """Build the index, or leave the old one on disk marked `status: "stale"` (with the
    reason) so the app never mistakes it for a fresh one."""
    result = _build(pois, index_dir, embed=embed, cache_path=cache_path, model=model, dim=dim)
    if result.status == "skipped":
        mark_stale(index_dir, result.reason)
    return result


def mark_stale(index_dir: Path, reason: str) -> bool:
    """Flag an existing index whose rebuild failed; True when there was one to flag."""
    meta_path = index_dir / "meta.json"
    if not meta_path.exists():
        return False
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta.update(
        status="stale",
        stale_reason=reason,
        stale_since=datetime.now(ASTANA_TZ).isoformat(timespec="seconds"),
    )
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return True


def ids_hash(pois: Sequence[POI]) -> str:
    return hashlib.sha256("\n".join(p.id for p in pois).encode("utf-8")).hexdigest()


def _build(
    pois: Sequence[POI],
    index_dir: Path,
    *,
    embed: EmbedFn | None,
    cache_path: Path | None,
    model: str | None,
    dim: int | None,
) -> IndexResult:
    settings = get_settings()
    model = model or settings.embed_model
    dim = dim or settings.embed_dim
    if embed is None:
        if not settings.has_embeddings:
            return IndexResult(
                "skipped", "нет GEMINI_API_KEY: индекс не строится, retrieval лексический"
            )
        embed = with_retries(gemini_embedder())
    cache_path = cache_path or settings.cache_dir / f"embeddings_{model}_{dim}.npz"
    cache = load_cache(cache_path)

    texts = [doc_text(p) for p in pois]
    keys = [text_key(t) for t in texts]
    todo = sorted({k: t for k, t in zip(keys, texts, strict=True) if k not in cache}.items())
    try:
        for start in range(0, len(todo), BATCH):
            batch = todo[start : start + BATCH]
            vectors = embed([t for _, t in batch])
            if len(vectors) != len(batch):
                raise ValueError(f"embedder returned {len(vectors)} vectors for {len(batch)}")
            for (key, _), vec in zip(batch, vectors, strict=True):
                cache[key] = np.asarray(vec, dtype=np.float32)
            log.info("embedded %d/%d", min(start + BATCH, len(todo)), len(todo))
    except Exception as exc:  # quota / network: keep what we have, do not fail the pipeline
        if cache:
            save_cache(cache_path, cache)
        missing = len({k for k in keys if k not in cache})
        return IndexResult("skipped", f"{failure_reason(exc)}; без вектора {missing} текстов")
    if todo:
        save_cache(cache_path, cache)

    matrix = np.stack([cache[k] for k in keys]).astype(np.float32)
    if matrix.shape[1] != dim:
        return IndexResult("skipped", f"размерность {matrix.shape[1]} ≠ EMBED_DIM={dim}")
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    zero = [pois[i].id for i in np.flatnonzero(norms[:, 0] == 0)]
    if zero:  # a broken embedder answer, not something to hide behind a 1.0 divisor
        raise ValueError(f"нулевой вектор эмбеддинга у {len(zero)} POI: {', '.join(zero[:5])}")
    matrix = matrix / norms

    index_dir.mkdir(parents=True, exist_ok=True)
    np.save(index_dir / "embeddings.npy", matrix.astype(np.float32))
    (index_dir / "ids.json").write_text(
        json.dumps([p.id for p in pois], ensure_ascii=False), encoding="utf-8"
    )
    meta = {
        "status": "ok",
        "model": model,
        "dim": dim,
        "count": len(pois),
        "created_at": datetime.now(ASTANA_TZ).isoformat(timespec="seconds"),
        "task_type": TASK_TYPE,
        "doc_template": DOC_TEMPLATE,
        "ids_sha256": ids_hash(pois),
        "docs_sha256": docs_hash(pois),
    }
    (index_dir / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return IndexResult("built", f"{len(pois)} векторов, {model}, dim={dim}", len(pois))


def docs_hash(pois: Sequence[POI]) -> str:
    """One hash over all (id, document text): tells whether the index matches poi.jsonl."""
    joined = "\n".join(f"{p.id}\t{doc_text(p)}" for p in pois)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def index_status(index_dir: Path, pois: Sequence[POI]) -> str:
    """One line for REPORT.md describing the index on disk."""
    meta_path = index_dir / "meta.json"
    if not meta_path.exists():
        return (
            "Векторный индекс не построен (нет `GEMINI_API_KEY` или квоты). "
            "Retrieval работает лексически по названиям, категориям и тегам."
        )
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    if meta.get("status") == "stale":
        stale = (
            f" — **устарел**: не пересобран ({meta.get('stale_reason')}); "
            f"в poi.jsonl {len(pois)} POI"
        )
    elif meta.get("count") != len(pois) or (
        meta.get("ids_sha256") and meta["ids_sha256"] != ids_hash(pois)
    ):
        stale = f" — **устарел**: в poi.jsonl {len(pois)} POI, id не совпадают"
    elif meta.get("docs_sha256") != docs_hash(pois):
        stale = (
            " — **устарел**: тексты POI (описания) изменились после сборки индекса; "
            "id и порядок совпадают, векторы части мест считаны по старым текстам"
        )
    else:
        stale = ""
    return (
        f"Векторный индекс: {meta.get('count')} векторов, модель `{meta.get('model')}`, "
        f"dim={meta.get('dim')}, создан {meta.get('created_at')}{stale}."
    )


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.parse_args(argv)
    logging.basicConfig(level=get_settings().log_level, format="%(levelname)s %(message)s")
    settings = get_settings()
    with settings.poi_path.open(encoding="utf-8") as fh:
        pois = [POI.model_validate_json(line) for line in fh if line.strip()]
    res = build_index(pois, settings.index_dir)
    print(f"index {res.status}: {res.reason}")


if __name__ == "__main__":
    main()
