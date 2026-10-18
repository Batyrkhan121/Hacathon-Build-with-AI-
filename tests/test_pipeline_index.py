"""pipeline.build_index with a fake embedder (no network, no API key)."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from app.schemas import POI
from pipeline.build_index import build_index, doc_text, index_status


def make_poi(i: int, **kw: object) -> POI:
    base = {
        "id": f"osm:node/{i}",
        "name": f"Место {i}",
        "category": "cafe",
        "tags": ["coffee", "indoor"],
        "geo": {"lat": 51.1, "lon": 71.4},
        "sources": ["osm"],
        "data_confidence": 0.5,
        "fetched_at": "2026-10-18",
    }
    return POI.model_validate(base | kw)


def fake_embedder(calls: list[list[str]], dim: int = 4):
    def embed(batch: list[str]) -> list[list[float]]:
        calls.append(batch)
        return [[float(len(t)), 1.0, 0.0, 2.0][:dim] for t in batch]

    return embed


def test_doc_text_template() -> None:
    poi = make_poi(1, description="Уютная кофейня.")
    assert doc_text(poi) == "Место 1. кафе, кофейня. Теги: coffee, indoor. Уютная кофейня."
    assert doc_text(make_poi(2, tags=[])).endswith("Теги: —.")


def test_build_writes_normalized_index_and_uses_cache(tmp_path: Path) -> None:
    pois = [make_poi(i) for i in range(3)]
    calls: list[list[str]] = []
    cache = tmp_path / "cache.npz"
    res = build_index(pois, tmp_path / "index", embed=fake_embedder(calls),
                      cache_path=cache, model="fake", dim=4)
    assert res.status == "built" and res.count == 3
    emb = np.load(tmp_path / "index" / "embeddings.npy")
    assert emb.dtype == np.float32 and emb.shape == (3, 4)
    assert np.allclose(np.linalg.norm(emb, axis=1), 1.0)
    ids = json.loads((tmp_path / "index" / "ids.json").read_text(encoding="utf-8"))
    assert ids == [p.id for p in pois]
    meta = json.loads((tmp_path / "index" / "meta.json").read_text(encoding="utf-8"))
    assert meta["count"] == 3 and meta["dim"] == 4 and "{name}" in meta["doc_template"]

    calls.clear()
    build_index(pois, tmp_path / "index", embed=fake_embedder(calls),
                cache_path=cache, model="fake", dim=4)
    assert calls == []  # everything came from the sha256 cache


def test_failure_skips_without_raising(tmp_path: Path) -> None:
    def broken(batch: list[str]) -> list[list[float]]:
        raise RuntimeError("quota exceeded")

    res = build_index([make_poi(1)], tmp_path / "index", embed=broken,
                      cache_path=tmp_path / "c.npz", model="fake", dim=4)
    assert res.status == "skipped" and "quota" in res.reason
    assert not (tmp_path / "index" / "meta.json").exists()
    assert "не построен" in index_status(tmp_path / "index", 1)


def test_no_key_skips(tmp_path: Path, monkeypatch) -> None:
    from app import config

    monkeypatch.setattr(config.Settings, "has_embeddings", property(lambda self: False))
    res = build_index([make_poi(1)], tmp_path / "index")
    assert res.status == "skipped" and "GEMINI_API_KEY" in res.reason
