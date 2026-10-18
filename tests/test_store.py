"""T22: Store loading — POI JSONL, landmarks, optional embedding index."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest

from app.config import Settings, get_settings
from app.schemas import POI
from app.store import load_landmarks, load_pois, load_store

from .conftest import FIXTURES, ROOT

SYNTHETIC = FIXTURES / "poi_synthetic.jsonl"
DIM = 4


def make_settings(tmp_path: Path, **overrides: object) -> Settings:
    base = replace(
        get_settings(),
        poi_path=SYNTHETIC,
        index_dir=tmp_path / "index",
        landmarks_path=ROOT / "data" / "landmarks.json",
        embed_model="test-embed",
        embed_dim=DIM,
    )
    return replace(base, **overrides)


def write_index(index_dir: Path, ids: list[str], matrix: np.ndarray, **meta: object) -> None:
    index_dir.mkdir(parents=True, exist_ok=True)
    np.save(index_dir / "embeddings.npy", matrix.astype(np.float32))
    (index_dir / "ids.json").write_text(json.dumps(ids), encoding="utf-8")
    info = {"model": "test-embed", "dim": DIM, "count": len(ids), "created_at": "2026-10-18"}
    info.update(meta)
    (index_dir / "meta.json").write_text(json.dumps(info), encoding="utf-8")


def test_load_pois_synthetic() -> None:
    pois = load_pois(SYNTHETIC)
    assert len(pois) == 14
    assert all(isinstance(p, POI) for p in pois)


def test_load_pois_skips_bad_rows(tmp_path: Path) -> None:
    good = SYNTHETIC.read_text(encoding="utf-8").splitlines()[:2]
    path = tmp_path / "poi.jsonl"
    path.write_text(
        "\n".join([good[0], "", "{not json", '{"id": "bad"}', good[0], good[1]]) + "\n",
        encoding="utf-8",
    )
    pois = load_pois(path)
    assert [p.id for p in pois] == ["test:poi/1", "test:poi/2"]  # duplicate dropped too


def test_load_pois_missing_file(tmp_path: Path) -> None:
    assert load_pois(tmp_path / "nope.jsonl") == []


def test_load_landmarks() -> None:
    landmarks = load_landmarks(ROOT / "data" / "landmarks.json")
    assert "expo" in landmarks
    assert landmarks["expo"].id == "expo"


def test_load_landmarks_skips_invalid(tmp_path: Path) -> None:
    path = tmp_path / "landmarks.json"
    rows = [
        {"id": "ok", "names": {"ru": "Ок"}, "geo": {"lat": 51.1, "lon": 71.4}},
        {"id": "Bad Id", "names": {}, "geo": {"lat": 51.1, "lon": 71.4}},
    ]
    path.write_text(json.dumps(rows), encoding="utf-8")
    assert list(load_landmarks(path)) == ["ok"]
    assert load_landmarks(tmp_path / "missing.json") == {}


def test_load_store_without_index_is_lexical(tmp_path: Path) -> None:
    store = load_store(make_settings(tmp_path))
    assert len(store.pois) == 14
    assert store.by_id["test:poi/3"].category == "museum"
    assert "expo" in store.landmarks
    assert store.embeddings is None
    assert store.snapshot_date == "2026-10-18"
    assert any("индекс" in w for w in store.warnings)


def test_load_store_aligns_embeddings_by_ids(tmp_path: Path) -> None:
    pois = load_pois(SYNTHETIC)
    ids = [p.id for p in pois][::-1][:-1]  # reversed order, first POI missing
    matrix = np.zeros((len(ids), DIM), dtype=np.float32)
    for row, poi_id in enumerate(ids):
        matrix[row, int(poi_id.rsplit("/", 1)[1]) % DIM] = 2.0  # not normalised on purpose
    write_index(tmp_path / "index", ids, matrix)

    store = load_store(make_settings(tmp_path))
    assert store.embeddings is not None
    assert store.embeddings.shape == (14, DIM)
    assert store.embed_model == "test-embed"
    for i, poi in enumerate(store.pois):
        if poi.id == "test:poi/1":
            assert not store.embeddings[i].any()  # missing row stays zero
            continue
        expected = np.zeros(DIM, dtype=np.float32)
        expected[int(poi.id.rsplit("/", 1)[1]) % DIM] = 1.0
        np.testing.assert_allclose(store.embeddings[i], expected)
    assert any("без эмбеддинга" in w for w in store.warnings)


@pytest.mark.parametrize("meta", [{"dim": 8}, {"model": "other-model"}])
def test_load_store_rejects_mismatched_index(tmp_path: Path, meta: dict[str, object]) -> None:
    ids = [p.id for p in load_pois(SYNTHETIC)]
    write_index(tmp_path / "index", ids, np.ones((len(ids), DIM)), **meta)
    store = load_store(make_settings(tmp_path))
    assert store.embeddings is None
    assert store.warnings


def test_load_store_poi_path_override(tmp_path: Path) -> None:
    path = tmp_path / "two.jsonl"
    path.write_text(
        "\n".join(SYNTHETIC.read_text(encoding="utf-8").splitlines()[:2]), encoding="utf-8"
    )
    store = load_store(make_settings(tmp_path), poi_path=path)
    assert len(store.pois) == 2


def test_load_store_missing_poi_file(tmp_path: Path) -> None:
    store = load_store(make_settings(tmp_path, poi_path=tmp_path / "none.jsonl"))
    assert store.pois == []
    assert store.snapshot_date is None
    assert any("POI" in w for w in store.warnings)


# --------------------------------------------------- broken files never crash


def test_load_pois_with_bom(tmp_path: Path) -> None:
    path = tmp_path / "bom.jsonl"
    path.write_text(SYNTHETIC.read_text(encoding="utf-8"), encoding="utf-8-sig")
    assert len(load_pois(path)) == 14


def test_load_landmarks_with_bom(tmp_path: Path) -> None:
    path = tmp_path / "landmarks.json"
    raw = (ROOT / "data" / "landmarks.json").read_text(encoding="utf-8")
    path.write_text(raw, encoding="utf-8-sig")
    assert "expo" in load_landmarks(path)


@pytest.mark.parametrize("encoding", ["cp1251", "utf-16"])
def test_load_store_bad_encoding(tmp_path: Path, encoding: str) -> None:
    path = tmp_path / "poi.jsonl"
    path.write_bytes(SYNTHETIC.read_text(encoding="utf-8").encode(encoding))
    store = load_store(make_settings(tmp_path, poi_path=path))
    assert store.pois == []
    assert any("не читается" in w for w in store.warnings)


def test_load_store_poi_path_is_directory(tmp_path: Path) -> None:
    store = load_store(make_settings(tmp_path, poi_path=tmp_path))
    assert store.pois == []
    assert store.warnings


def _broken_index(index_dir: Path, case: str) -> None:
    ids = [p.id for p in load_pois(SYNTHETIC)]
    write_index(index_dir, ids, np.ones((len(ids), DIM)))
    if case == "empty_npy":
        (index_dir / "embeddings.npy").write_bytes(b"")
    elif case == "meta_list":
        (index_dir / "meta.json").write_text("[]", encoding="utf-8")
    elif case == "ids_null":
        (index_dir / "ids.json").write_text("null", encoding="utf-8")
    elif case == "ids_nested":
        (index_dir / "ids.json").write_text("[[1]]", encoding="utf-8")
    elif case == "string_matrix":
        np.save(index_dir / "embeddings.npy", np.array([["a"] * DIM] * len(ids)))


@pytest.mark.parametrize(
    "case", ["empty_npy", "meta_list", "ids_null", "ids_nested", "string_matrix"]
)
def test_load_store_broken_index_falls_back(tmp_path: Path, case: str) -> None:
    _broken_index(tmp_path / "index", case)
    store = load_store(make_settings(tmp_path))
    assert len(store.pois) == 14
    assert store.embeddings is None
    assert store.warnings


def test_load_store_non_finite_rows_are_missing(tmp_path: Path) -> None:
    ids = [p.id for p in load_pois(SYNTHETIC)]
    matrix = np.ones((len(ids), DIM), dtype=np.float32)
    matrix[0, 0] = np.nan
    write_index(tmp_path / "index", ids, matrix)
    store = load_store(make_settings(tmp_path))
    assert store.embeddings is not None
    assert np.isfinite(store.embeddings).all()
    assert not store.embeddings[0].any()


# -------------------------------- index status from meta.json (store is QUALITY, wave 3)


def _ids_sha256(ids: list[str]) -> str:
    import hashlib

    return hashlib.sha256("\n".join(ids).encode("utf-8")).hexdigest()  # as pipeline.build_index


def _full_index(tmp_path: Path, **meta: object) -> None:
    ids = [p.id for p in load_pois(SYNTHETIC)]
    write_index(tmp_path / "index", ids, np.ones((len(ids), DIM)), **meta)


def test_stale_index_warns_but_vectors_are_used(tmp_path: Path) -> None:
    _full_index(tmp_path, status="stale", stale_reason="дневная квота Gemini исчерпана",
                stale_since="2026-10-18T17:50:14+05:00")
    store = load_store(make_settings(tmp_path))
    assert store.embeddings is not None
    stale = [w for w in store.warnings if "устарел" in w]
    assert stale and "квота" in stale[0] and "2026-10-18" in stale[0]


def test_index_without_status_is_old_format_not_stale(tmp_path: Path) -> None:
    _full_index(tmp_path)  # meta written before DATA added status
    store = load_store(make_settings(tmp_path))
    assert store.embeddings is not None
    assert not any("устарел" in w for w in store.warnings)


def test_ok_index_with_matching_count_and_ids_is_quiet(tmp_path: Path) -> None:
    ids = [p.id for p in load_pois(SYNTHETIC)]
    _full_index(tmp_path, status="ok", count=len(ids), ids_sha256=_ids_sha256(ids))
    store = load_store(make_settings(tmp_path))
    assert store.embeddings is not None
    assert store.warnings == []


def test_index_count_mismatch_is_a_warning(tmp_path: Path) -> None:
    _full_index(tmp_path, status="ok", count=999)
    store = load_store(make_settings(tmp_path))
    assert store.embeddings is not None
    assert any("999" in w and "14" in w for w in store.warnings)


def test_index_ids_hash_mismatch_is_a_warning(tmp_path: Path) -> None:
    ids = [p.id for p in load_pois(SYNTHETIC)]
    _full_index(tmp_path, status="ok", count=len(ids), ids_sha256="0" * 64)
    store = load_store(make_settings(tmp_path))
    assert store.embeddings is not None
    assert any("id" in w for w in store.warnings)
