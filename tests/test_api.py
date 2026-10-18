"""app/api.py via TestClient on the synthetic Store, CORE fakes and a fake LLM (no network)."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path

import h3
import pytest
from fastapi.testclient import TestClient

from app import api
from app.llm import LLMUnavailable
from app.pipeline import Deps
from app.schemas import POI, Answer, Landmark

from .test_pipeline import FakeLLM, install_core_fakes, make_settings, make_store


@pytest.fixture
def deps(synthetic_pois: list[POI], landmarks: dict[str, Landmark], tmp_path: Path) -> Deps:
    return Deps(store=make_store(synthetic_pois, landmarks), llm=FakeLLM(),
                settings=make_settings(tmp_path))


@pytest.fixture
def client(deps: Deps, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    install_core_fakes(monkeypatch)
    with TestClient(api.create_app(deps)) as c:
        yield c


def body(**kw: object) -> dict[str, object]:
    data: dict[str, object] = {"query": "кофе", "location": {"landmark_id": "expo"}}
    data.update(kw)
    return data


def test_health(client: TestClient) -> None:
    r = client.get("/api/health")
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "ok" and data["poi_count"] == 14
    assert data["index"] == "lexical" and data["llm"] == "ok"


def test_health_llm_off(deps: Deps, monkeypatch: pytest.MonkeyPatch) -> None:
    deps.llm = FakeLLM(available=False)
    with TestClient(api.create_app(deps)) as c:
        assert c.get("/api/health").json()["llm"] == "off (cooldown)"  # with the reason


def test_health_degraded_without_pois(landmarks: dict[str, Landmark], tmp_path: Path) -> None:
    d = Deps(store=make_store([], landmarks, warnings=["poi.jsonl: 0 valid rows"]),
             llm=FakeLLM(), settings=make_settings(tmp_path))
    with TestClient(api.create_app(d)) as c:
        h = c.get("/api/health")
        assert h.status_code == 200
        body_ = h.json()
        assert body_["status"] == "degraded" and body_["poi_count"] == 0
        assert "poi.jsonl: 0 valid rows" in body_["warnings"]
        assert any("POI" in w for w in body_["warnings"])
        meta = c.get("/api/meta").json()
        assert meta["status"] == "degraded" and meta["warnings"]
        r = c.post("/api/recommend", json=body())
        assert r.status_code == 503 and "POI" in r.json()["detail"]


def test_health_degraded_without_landmarks(synthetic_pois: list[POI], tmp_path: Path) -> None:
    d = Deps(store=make_store(synthetic_pois, {}), llm=FakeLLM(),
             settings=make_settings(tmp_path))
    with TestClient(api.create_app(d)) as c:
        h = c.get("/api/health").json()
    assert h["status"] == "degraded" and any("ориентир" in w for w in h["warnings"])


def test_health_explains_index_and_llm(deps: Deps) -> None:
    import dataclasses

    import numpy as np

    deps.store.embeddings = np.zeros((len(deps.store.pois), 4), dtype=np.float32)
    deps.settings = dataclasses.replace(deps.settings, gemini_api_key=None)
    deps.llm = FakeLLM(available=False)
    with TestClient(api.create_app(deps)) as c:
        h = c.get("/api/health").json()
    assert h["index"] == "ready (no key)"
    assert h["llm"].startswith("off (")
    deps.settings = dataclasses.replace(deps.settings, gemini_api_key="g-key")
    deps.llm = FakeLLM()
    with TestClient(api.create_app(deps)) as c:
        h = c.get("/api/health").json()
    assert h["index"] == "ready" and h["llm"] == "ok" and h["status"] == "ok"


def test_missing_prompt_is_loud_at_startup(deps: Deps, monkeypatch: pytest.MonkeyPatch,
                                           caplog: pytest.LogCaptureFixture) -> None:
    from app import generate

    def missing() -> None:
        raise FileNotFoundError("prompts/generate_cards.md")

    monkeypatch.setattr(generate, "check_prompt", missing)
    with caplog.at_level(logging.ERROR), TestClient(api.create_app(deps)) as c:
        h = c.get("/api/health").json()
    assert h["status"] == "degraded"
    assert any("generate_cards" in w for w in h["warnings"])
    assert any("prompt" in r.getMessage() for r in caplog.records if r.levelname == "ERROR")


def test_stale_index_makes_health_degraded(deps: Deps, tmp_path: Path) -> None:
    import dataclasses
    import json

    index = tmp_path / "index"
    index.mkdir()
    (index / "meta.json").write_text(json.dumps({"status": "stale", "stale_reason": "квота"}),
                                     encoding="utf-8")
    deps.settings = dataclasses.replace(deps.settings, index_dir=index)
    deps.store.warnings = ["индекс эмбеддингов устарел (с 2026-10-19): квота; векторы используются"]
    with TestClient(api.create_app(deps)) as c:
        h = c.get("/api/health").json()
        m = c.get("/api/meta").json()
    assert h["status"] == "degraded" and m["status"] == "degraded"
    assert any("устарел" in w for w in h["warnings"])
    (index / "meta.json").write_text(json.dumps({"status": "ok"}), encoding="utf-8")
    deps.store.warnings = []
    with TestClient(api.create_app(deps)) as c:
        assert c.get("/api/health").json()["status"] == "ok"


def test_landmarks(client: TestClient, landmarks: dict[str, Landmark]) -> None:
    r = client.get("/api/landmarks")
    assert r.status_code == 200
    ids = [x["id"] for x in r.json()]
    assert ids == sorted(landmarks) and "expo" in ids


def test_meta(client: TestClient) -> None:
    data = client.get("/api/meta").json()
    assert data["poi_count"] == 14 and data["snapshot_date"] == "2026-10-18"
    assert sum(data["by_category"].values()) == 14
    assert data["sources"] == ["manual"]


def test_recommend_llm(client: TestClient) -> None:
    r = client.post("/api/recommend", json=body(query="Кофе у EXPO"))
    assert r.status_code == 200, r.text
    answer = Answer.model_validate(r.json())
    assert answer.data_used.generation == "llm"
    assert 1 <= len(answer.cards) <= 3


def test_recommend_without_ai(client: TestClient) -> None:
    r = client.post("/api/recommend", json=body(use_llm=False))
    answer = Answer.model_validate(r.json())
    assert answer.data_used.generation == "template" and answer.data_used.parser == "rules"


def test_recommend_llm_failure_still_200(deps: Deps, monkeypatch: pytest.MonkeyPatch) -> None:
    install_core_fakes(monkeypatch)
    deps.llm = FakeLLM(fail=LLMUnavailable("quota"))
    with TestClient(api.create_app(deps)) as c:
        r = c.post("/api/recommend", json=body())
    assert r.status_code == 200
    assert "LLM недоступна (нет связи) — показаны карточки по данным." in r.json()["warnings"]


def test_unknown_landmark_422(client: TestClient) -> None:
    r = client.post("/api/recommend", json=body(location={"landmark_id": "nowhere"}))
    assert r.status_code == 422
    assert "Неизвестный ориентир" in r.json()["detail"]


def test_invalid_h3_cell_422(client: TestClient) -> None:
    r = client.post("/api/recommend", json=body(location={"h3_cell": "000000000000000"}))
    assert r.status_code == 422


@pytest.mark.parametrize(
    "payload",
    [
        {"query": "кофе", "location": {"lat": 51.1, "lon": 71.4}},
        {"query": "кофе", "location": {"landmark_id": "expo", "h3_cell": "892153806d3ffff"}},
        {"query": "кофе", "location": {"landmark_id": "expo"}, "lat": 51.1},
        {"query": "", "location": {"landmark_id": "expo"}},
        {"query": "x" * 501, "location": {"landmark_id": "expo"}},
    ],
    ids=["raw-coords", "two-locations", "extra-field", "empty-query", "long-query"],
)
def test_invalid_payloads_422(client: TestClient, payload: dict) -> None:
    assert client.post("/api/recommend", json=payload).status_code == 422


def test_absurd_time_is_422(client: TestClient) -> None:
    r = client.post("/api/recommend", json=body(at="0001-01-01T00:00:00+14:00"))
    assert r.status_code == 422 and "at вне" in r.json()["detail"]


def test_oversized_body_is_413(client: TestClient) -> None:
    r = client.post("/api/recommend", json=body(location={"landmark_id": "a" * 20_000}))
    assert r.status_code == 413
    assert len(r.content) < 500  # the body is not echoed back


def test_chunked_oversized_body_is_413(client: TestClient) -> None:
    def chunks() -> object:
        yield b'{"query": "'
        for _ in range(40):
            yield b"x" * 1024
        yield b'"}'

    r = client.post("/api/recommend", content=chunks(),
                    headers={"Content-Type": "application/json"})
    assert r.status_code == 413


def test_busy_server_answers_without_ai(deps: Deps, monkeypatch: pytest.MonkeyPatch) -> None:
    install_core_fakes(monkeypatch)
    monkeypatch.setattr(api, "MAX_CONCURRENT_LLM", 0)  # every LLM slot is taken
    with TestClient(api.create_app(deps)) as c:
        data = c.post("/api/recommend", json=body()).json()
    assert data["data_used"]["generation"] == "template"
    assert data["warnings"][0] == "Сервер занят — этот ответ собран без ИИ."
    assert deps.llm.calls == []


def test_openapi_documents_errors(client: TestClient) -> None:
    spec = client.get("/openapi.json").json()
    responses = spec["paths"]["/api/recommend"]["post"]["responses"]
    assert {"200", "413", "422", "500", "503"} <= set(responses)


def test_h3_cell_request(client: TestClient) -> None:
    cell = h3.latlng_to_cell(51.0894, 71.4161, 9)
    r = client.post("/api/recommend", json=body(location={"h3_cell": cell}, use_llm=False))
    assert r.status_code == 200
    assert r.json()["context"]["h3_cell"] == cell


def test_unexpected_error_is_500_without_leaking(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    secret = "секретный запрос"

    def boom(req: object, deps: object) -> None:
        raise RuntimeError(f"bug while handling {secret}")

    monkeypatch.setattr(api, "recommend", boom)
    with caplog.at_level(logging.INFO):
        r = client.post("/api/recommend", json=body(query=secret))
    assert r.status_code == 500
    assert secret not in r.text
    logged = "\n".join(rec.getMessage() for rec in caplog.records)
    assert "recommend failed: RuntimeError" in logged
    assert secret not in logged


def test_access_log_has_no_body(client: TestClient, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="qadam.api"):
        client.post("/api/recommend", json=body(query="уникальный текст 42", use_llm=False))
    logged = "\n".join(rec.getMessage() for rec in caplog.records)
    assert "POST /api/recommend -> 200" in logged
    assert "уникальный текст 42" not in logged


def test_static_web_is_mounted_after_api(client: TestClient) -> None:
    r = client.get("/mock/answer_sample.json")
    assert r.status_code == 200 and "cards" in r.json()
    assert client.get("/api/health").status_code == 200  # not shadowed by StaticFiles
    assert client.get("/api/nope").status_code == 404


def test_web_files_are_not_cached(client: TestClient) -> None:
    first = client.get("/")
    assert first.status_code == 200 and first.headers["cache-control"] == "no-cache"
    js = client.get("/app.js")
    assert js.status_code == 200 and js.headers["cache-control"] == "no-cache"
    etag = js.headers.get("etag")
    if etag:  # revalidation stays cheap
        again = client.get("/app.js", headers={"If-None-Match": etag})
        assert again.status_code == 304 and again.headers["cache-control"] == "no-cache"


def test_cors_for_dev_ui(client: TestClient) -> None:
    r = client.options("/api/recommend", headers={
        "Origin": "http://localhost:5500", "Access-Control-Request-Method": "POST"})
    assert r.headers.get("access-control-allow-origin") == "http://localhost:5500"
    r = client.options("/api/recommend", headers={
        "Origin": "https://evil.example", "Access-Control-Request-Method": "POST"})
    assert "access-control-allow-origin" not in r.headers


def test_store_load_failure_degrades(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken() -> Deps:
        raise FileNotFoundError("data/processed/poi.jsonl")

    monkeypatch.setattr(api, "build_default_deps", broken)
    with TestClient(api.create_app()) as c:
        h = c.get("/api/health")
        assert h.status_code == 503 and h.json()["status"] == "unavailable"
        assert c.get("/api/landmarks").status_code == 503
        assert c.post("/api/recommend", json=body()).status_code == 503
        assert c.get("/mock/answer_sample.json").status_code == 200  # UI still served


def test_default_deps_are_loaded_in_lifespan(
    deps: Deps, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[int] = []

    def fake_build() -> Deps:
        calls.append(1)
        return deps

    monkeypatch.setattr(api, "build_default_deps", fake_build)
    app = api.create_app()
    assert calls == []  # nothing is loaded at import / creation time
    with TestClient(app) as c:
        assert c.get("/api/health").json()["poi_count"] == 14
    assert calls == [1]
