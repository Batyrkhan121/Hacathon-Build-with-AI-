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
    assert "не построен" in index_status(tmp_path / "index", [make_poi(1)])


def test_no_key_skips(tmp_path: Path, monkeypatch) -> None:
    from app import config

    monkeypatch.setattr(config.Settings, "has_embeddings", property(lambda self: False))
    res = build_index([make_poi(1)], tmp_path / "index")
    assert res.status == "skipped" and "GEMINI_API_KEY" in res.reason


def test_status_flags_index_built_from_old_texts(tmp_path: Path) -> None:
    pois = [make_poi(i) for i in range(2)]
    build_index(pois, tmp_path / "index", embed=fake_embedder([]),
                cache_path=tmp_path / "c.npz", model="fake", dim=4)
    assert "устарел" not in index_status(tmp_path / "index", pois)
    changed = [pois[0].model_copy(update={"description": "Новое описание."}), pois[1]]
    assert "тексты POI (описания) изменились" in index_status(tmp_path / "index", changed)
    assert "в poi.jsonl 1 POI" in index_status(tmp_path / "index", pois[:1])


def test_daily_quota_is_not_retried_and_reported_briefly(tmp_path: Path) -> None:
    from pipeline.build_index import with_retries

    calls: list[int] = []

    def exhausted(batch: list[str]) -> list[list[float]]:
        calls.append(1)
        raise RuntimeError("429 RESOURCE_EXHAUSTED {'quotaId': "
                           "'EmbedContentRequestsPerDayPerUserPerProjectPerModel-FreeTier'}")

    res = build_index([make_poi(1)], tmp_path / "index",
                      embed=with_retries(exhausted, sleep=lambda s: None),
                      cache_path=tmp_path / "c.npz", model="fake", dim=4)
    assert calls == [1]
    assert res.status == "skipped" and "дневная квота Gemini" in res.reason
    assert "без вектора 1 текстов" in res.reason and "{" not in res.reason


def test_failed_rebuild_marks_the_old_index_stale(tmp_path: Path) -> None:
    import hashlib

    pois = [make_poi(i) for i in range(3)]
    index = tmp_path / "index"
    build_index(pois, index, embed=fake_embedder([]), cache_path=tmp_path / "c.npz",
                model="fake", dim=4)
    meta = json.loads((index / "meta.json").read_text(encoding="utf-8"))
    assert meta["status"] == "ok" and meta["count"] == 3
    ids_hash = hashlib.sha256("\n".join(p.id for p in pois).encode("utf-8")).hexdigest()
    assert meta["ids_sha256"] == ids_hash and meta["docs_sha256"]

    def broken(batch: list[str]) -> list[list[float]]:
        raise RuntimeError("network down")

    more = [*pois, make_poi(9)]
    res = build_index(more, index, embed=broken, cache_path=tmp_path / "c.npz",
                      model="fake", dim=4)
    assert res.status == "skipped"
    meta = json.loads((index / "meta.json").read_text(encoding="utf-8"))
    assert meta["status"] == "stale" and "network down" in meta["stale_reason"]
    assert meta["count"] == 3  # the old index is kept for lexical+semantic fallback, but flagged
    status = index_status(index, more)
    assert "устарел" in status and "не пересобран (" in status and "network down" in status


def test_zero_vector_fails_loudly(tmp_path: Path) -> None:
    import pytest

    def zeros(batch: list[str]) -> list[list[float]]:
        return [[0.0, 0.0, 0.0, 0.0] for _ in batch]

    with pytest.raises(ValueError, match="нулевой"):
        build_index([make_poi(1)], tmp_path / "index", embed=zeros,
                    cache_path=tmp_path / "c.npz", model="fake", dim=4)
    assert not (tmp_path / "index" / "meta.json").exists()


# ------------------------------------------------------------- OpenRouter (no network)


def _openrouter(handler, dim: int = 4):
    import httpx

    from pipeline.build_index import openrouter_embedder

    client = httpx.Client(transport=httpx.MockTransport(handler))
    return openrouter_embedder("test-key", "google/gemini-embedding-001", dim, client=client)


def test_openrouter_request_shape_order_and_truncation() -> None:
    seen: dict = {}

    def handler(request):
        import httpx

        seen["url"] = str(request.url)
        seen["auth_ok"] = request.headers.get("authorization") == "Bearer test-key"
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"data": [
            {"index": 1, "embedding": [0.0, 3.0, 0.0, 4.0, 9.0, 9.0]},  # 6 > dim 4
            {"index": 0, "embedding": [1.0, 0.0, 0.0, 0.0, 5.0, 5.0]},
        ]})

    vectors = _openrouter(handler)(["first", "second"])
    assert seen["url"] == "https://openrouter.ai/api/v1/embeddings" and seen["auth_ok"]
    body = seen["body"]
    assert body["model"] == "google/gemini-embedding-001" and body["input"] == ["first", "second"]
    assert body["encoding_format"] == "float" and body["dimensions"] == 4
    assert body["provider"] == {"data_collection": "deny"} and "task_type" not in body
    assert vectors[0] == [1.0, 0.0, 0.0, 0.0]  # index 0 first, truncated to dim
    assert np.allclose(vectors[1], [0.0, 0.6, 0.0, 0.8])  # truncated then L2-normalized


def test_openrouter_short_vector_is_an_error() -> None:
    import httpx
    import pytest

    short = _openrouter(lambda r: httpx.Response(200, json={"data": [
        {"index": 0, "embedding": [1.0, 0.0]}]}))
    with pytest.raises(ValueError, match="размерност"):
        short(["x"])


def test_openrouter_401_402_not_retried_429_retried(tmp_path: Path) -> None:
    import httpx

    from pipeline.build_index import with_retries

    for status, reason in ((401, "ключ не принят"), (402, "недостаточно средств"),
                           (429, "лимит запросов OpenRouter")):
        calls: list[int] = []

        def handler(request, status=status, calls=calls):
            calls.append(1)
            return httpx.Response(status, json={"error": {"code": status, "message": "nope"}})

        embed = with_retries(_openrouter(handler), pauses=(1.0, 2.0), sleep=lambda s: None)
        res = build_index([make_poi(1)], tmp_path / f"i{status}", embed=embed,
                          cache_path=tmp_path / f"c{status}.npz", model="m", dim=4)
        assert res.status == "skipped" and reason in res.reason, res.reason
        assert len(calls) == (3 if status == 429 else 1)
        assert "test-key" not in res.reason


def test_embedding_caches_are_separate_per_provider_model_and_dim(
    tmp_path: Path, monkeypatch
) -> None:
    from app.config import get_settings
    from pipeline.build_index import cache_file

    assert len({cache_file("gemini", "gemini-embedding-001", 768),
                cache_file("openrouter", "google/gemini-embedding-001", 768),
                cache_file("openrouter", "google/gemini-embedding-001", 1536)}) == 3
    assert "/" not in cache_file("openrouter", "google/gemini-embedding-001", 768)

    monkeypatch.setenv("QADAM_DATA_DIR", str(tmp_path))
    for name in ("QADAM_CACHE_DIR", "QADAM_INDEX_DIR", "QADAM_POI_PATH"):
        monkeypatch.delenv(name, raising=False)
    get_settings.cache_clear()
    try:
        pois = [make_poi(i) for i in range(3)]
        first: list[list[str]] = []
        build_index(pois, tmp_path / "index", embed=fake_embedder(first), model="m", dim=4,
                    provider="gemini")
        second: list[list[str]] = []
        build_index(pois, tmp_path / "index", embed=fake_embedder(second),
                    model="google/m", dim=4, provider="openrouter")
        assert sum(len(b) for b in second) == 3  # nothing reused across providers/models
        meta = json.loads((tmp_path / "index" / "meta.json").read_text(encoding="utf-8"))
        assert meta["provider"] == "openrouter" and meta["model"] == "google/m"
        assert meta["task_type"] == "none (openrouter)"
    finally:
        get_settings.cache_clear()
