"""app/api.py via TestClient on the synthetic Store, CORE fakes and a fake LLM (no network)."""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Iterator
from pathlib import Path

import h3
import httpx
import pytest
from fastapi.testclient import TestClient

from app import api
from app.llm import LLMUnavailable
from app.pipeline import Deps
from app.schemas import POI, Answer, Landmark, RecommendRequest

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


def private_error_body(response: httpx.Response, caplog: pytest.LogCaptureFixture,
                       marker: str, cell: str) -> dict:
    def reject_constant(value: str) -> None:
        pytest.fail(f"Non-finite value in error response: {value}")

    data = json.loads(response.text, parse_constant=reject_constant)
    for text in (response.text, caplog.text):
        assert marker not in text
        assert cell not in text
    assert "traceback" not in response.text.casefold()
    assert "site-packages" not in response.text
    assert str(Path(__file__).resolve().parent.parent) not in response.text
    return data


def assert_safe_request_error(response: httpx.Response, caplog: pytest.LogCaptureFixture,
                              marker: str, cell: str) -> None:
    assert response.status_code == 422
    data = private_error_body(response, caplog, marker, cell)
    assert data == {"detail": [{"type": "request_validation", "loc": ["body"],
                               "msg": "Неверный формат запроса."}]}
    assert all("input" not in error and "ctx" not in error for error in data["detail"])
    assert "request validation failed" in caplog.text
    assert "POST /api/recommend -> 422" in caplog.text
    assert not any(record.exc_info for record in caplog.records)


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
        # 503: a hosting platform's HTTP check only sees the code, an empty server must fail it
        assert h.status_code == 503
        body_ = h.json()
        assert body_["status"] == "degraded" and body_["poi_count"] == 0
        assert body_["index"] == "lexical" and body_["llm"] == "ok"
        assert body_["schema_version"] == api.SCHEMA_VERSION
        assert "poi.jsonl: 0 valid rows" in body_["warnings"]
        assert any("POI" in w for w in body_["warnings"])
        meta = c.get("/api/meta")  # data description, not a probe: the code stays 200
        assert meta.status_code == 200
        assert meta.json()["status"] == "degraded" and meta.json()["warnings"]
        r = c.post("/api/recommend", json=body())
        assert r.status_code == 503 and "POI" in r.json()["detail"]


def test_health_degraded_without_landmarks(synthetic_pois: list[POI], tmp_path: Path) -> None:
    d = Deps(store=make_store(synthetic_pois, {}), llm=FakeLLM(),
             settings=make_settings(tmp_path))
    with TestClient(api.create_app(d)) as c:
        r = c.get("/api/health")
    h = r.json()
    assert h["status"] == "degraded" and any("ориентир" in w for w in h["warnings"])
    assert r.status_code == 200  # degraded with POI: the server answers, cards work


def test_degraded_with_pois_is_still_200(deps: Deps, tmp_path: Path) -> None:
    # LLM off + stale index + another model: all degraded, none of them is an empty server
    import dataclasses
    import json

    index = tmp_path / "index"
    index.mkdir()
    (index / "meta.json").write_text(json.dumps({"model": "gemini-embedding-001", "dim": 768,
                                                 "status": "stale"}), encoding="utf-8")
    deps.settings = dataclasses.replace(
        deps.settings, index_dir=index, embed_provider="openrouter", openrouter_api_key="k",
        embed_model="google/gemini-embedding-001", embed_dim=768)
    deps.llm = FakeLLM(available=False)
    with TestClient(api.create_app(deps)) as c:
        r = c.get("/api/health")
    assert r.status_code == 200
    assert r.json()["status"] == "degraded" and r.json()["poi_count"] == 14


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


def test_index_of_another_model_makes_health_degraded(deps: Deps, tmp_path: Path) -> None:
    import dataclasses
    import json

    index = tmp_path / "index"
    index.mkdir()
    (index / "meta.json").write_text(json.dumps({"model": "gemini-embedding-001", "dim": 768}),
                                     encoding="utf-8")
    deps.settings = dataclasses.replace(
        deps.settings, index_dir=index, embed_provider="openrouter", openrouter_api_key="k",
        embed_model="google/gemini-embedding-001", embed_dim=768)
    deps.store.embeddings = None
    with TestClient(api.create_app(deps)) as c:
        h = c.get("/api/health").json()
    assert h["status"] == "degraded" and h["index"] == "lexical (index mismatch)"
    assert any("gemini-embedding-001/768" in w and "google/gemini-embedding-001/768" in w
               for w in h["warnings"])


def _stale_index(deps: Deps, tmp_path: Path, model: str) -> None:
    import dataclasses
    import json

    index = tmp_path / "index"
    index.mkdir()
    (index / "meta.json").write_text(json.dumps({"model": model, "dim": 768, "status": "stale",
                                                 "stale_reason": "квота"}), encoding="utf-8")
    deps.settings = dataclasses.replace(
        deps.settings, index_dir=index, embed_provider="openrouter", openrouter_api_key="k",
        embed_model="google/gemini-embedding-001", embed_dim=768)


def test_stale_index_of_another_model_is_not_called_stale(deps: Deps, tmp_path: Path) -> None:
    # the vectors are not used at all: «устарел» would only confuse, the mismatch is the news
    _stale_index(deps, tmp_path, model="gemini-embedding-001")
    deps.store.embeddings = None
    deps.store.warnings = ["индекс построен для gemini-embedding-001/768, а в настройках "
                           "google/gemini-embedding-001/768, релевантность считается лексически"]
    with TestClient(api.create_app(deps)) as c:
        h = c.get("/api/health").json()
        m = c.get("/api/meta").json()
    for data in (h, m):
        assert data["status"] == "degraded" and data["index"] == "lexical (index mismatch)"
        assert any("индекс построен" in w for w in data["warnings"])
        assert not any("устарел" in w for w in data["warnings"]), data["warnings"]


def test_stale_index_of_the_query_model_is_called_stale(deps: Deps, tmp_path: Path) -> None:
    import numpy as np

    _stale_index(deps, tmp_path, model="google/gemini-embedding-001")
    deps.store.embeddings = np.zeros((len(deps.store.pois), 768), dtype=np.float32)
    deps.store.warnings = []  # api reads the flag from meta.json, not from Store's text
    with TestClient(api.create_app(deps)) as c:
        h = c.get("/api/health").json()
    assert h["status"] == "degraded" and h["index"] == "ready"
    assert [w for w in h["warnings"] if "устарел" in w] == [
        "индекс эмбеддингов устарел (meta.json: status=stale)"]


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


@pytest.mark.parametrize("token", ["NaN", "Infinity", "-Infinity"])
@pytest.mark.parametrize("shape", ["override", "array"])
def test_nonfinite_request_errors_are_private_422(
    client: TestClient, caplog: pytest.LogCaptureFixture, token: str, shape: str
) -> None:
    marker = "qa-private-nonfinite-marker"
    cell = h3.latlng_to_cell(51.0894, 71.4161, 9)
    payload = body(query=marker, location={"h3_cell": cell}, use_llm=False)
    if shape == "override":
        payload["overrides"] = {"group_size": float(token)}
        raw = json.dumps(payload, allow_nan=True)
    else:
        raw = f"[{json.dumps(payload)}, {token}]"

    with caplog.at_level(logging.INFO):
        response = client.post("/api/recommend", content=raw,
                               headers={"Content-Type": "application/json"})
    assert_safe_request_error(response, caplog, marker, cell)


def test_extra_field_name_is_not_echoed_in_request_error(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    marker = "qa-private-extra-field-marker"
    cell = h3.latlng_to_cell(51.0894, 71.4161, 9)
    payload = body(query=marker, location={"h3_cell": cell}, use_llm=False)
    payload[f"{marker}_{cell}"] = True
    with caplog.at_level(logging.INFO):
        response = client.post("/api/recommend", json=payload)
    assert_safe_request_error(response, caplog, marker, cell)


def test_malformed_json_error_is_private_422(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    marker = "qa-private-malformed-marker"
    cell = h3.latlng_to_cell(51.0894, 71.4161, 9)
    raw = json.dumps(body(query=marker, location={"h3_cell": cell}, use_llm=False))[:-1]
    with caplog.at_level(logging.INFO):
        response = client.post("/api/recommend", content=raw,
                               headers={"Content-Type": "application/json"})
    assert_safe_request_error(response, caplog, marker, cell)


def test_finite_override_control_still_returns_answer(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    marker = "qa-private-valid-control-marker"
    cell = h3.latlng_to_cell(51.0894, 71.4161, 9)
    with caplog.at_level(logging.INFO):
        response = client.post("/api/recommend", json=body(
            query=marker, location={"h3_cell": cell}, overrides={"group_size": 2}, use_llm=False))
    assert response.status_code == 200
    answer = Answer.model_validate(response.json())
    assert answer.intent.group_size == 2
    assert answer.data_used.generation == "template" and answer.data_used.parser == "rules"
    assert marker not in caplog.text and cell not in caplog.text


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
    marker = "qa-private-runtime-error-marker"
    cell = h3.latlng_to_cell(51.0894, 71.4161, 9)

    def boom(req: RecommendRequest, deps: Deps) -> None:
        raise RuntimeError(f"bug while handling {req.query} at {req.location.h3_cell}")

    monkeypatch.setattr(api, "recommend", boom)
    with caplog.at_level(logging.INFO):
        r = client.post("/api/recommend", json=body(
            query=marker, location={"h3_cell": cell}, use_llm=False))
    assert r.status_code == 500
    assert private_error_body(r, caplog, marker, cell) == {"detail": api.INTERNAL_ERROR}
    assert "recommend failed: RuntimeError" in caplog.text


def test_response_validation_error_remains_private_500(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    marker = "qa-private-response-error-marker"
    cell = h3.latlng_to_cell(51.0894, 71.4161, 9)

    def invalid_answer(req: RecommendRequest, deps: Deps) -> dict:
        return {"summary": req.query, "context": {"h3_cell": req.location.h3_cell}}

    monkeypatch.setattr(api, "recommend", invalid_answer)
    with caplog.at_level(logging.INFO):
        response = client.post("/api/recommend", json=body(
            query=marker, location={"h3_cell": cell}, use_llm=False))
    assert response.status_code == 500
    assert private_error_body(response, caplog, marker, cell) == {"detail": api.INTERNAL_ERROR}
    assert "response validation failed" in caplog.text


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
    warmed: list[list[object]] = []
    monkeypatch.setattr(api.weather, "warm_up", lambda points: warmed.append(list(points)) or 0)
    app = api.create_app()
    assert calls == []  # nothing is loaded at import / creation time
    with TestClient(app) as c:
        assert c.get("/api/health").json()["poi_count"] == 14
    assert calls == [1]
    # T133: today's weather for the landmark areas is warmed up at start, in the background
    for _ in range(100):
        if warmed:
            break
        time.sleep(0.01)
    assert warmed and len(warmed[0]) == len(deps.store.landmarks)


def test_no_weather_warm_up_without_a_store(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken() -> Deps:
        raise FileNotFoundError("data/processed/poi.jsonl")

    warmed: list[int] = []
    monkeypatch.setattr(api, "build_default_deps", broken)
    monkeypatch.setattr(api.weather, "warm_up", lambda points: warmed.append(1) or 0)
    with TestClient(api.create_app()) as c:
        assert c.get("/api/health").status_code == 503
    assert warmed == []


# ------------------------------------------------------ T123: prefs through HTTP, never logged


def test_prefs_answer_through_http_and_stay_out_of_the_logs(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    from .test_pipeline import ALL_PREFS, EXCLUDE, _pref_leaks

    with caplog.at_level(logging.DEBUG):
        ok = client.post("/api/recommend", json=body(query="тихое место", prefs=ALL_PREFS,
                                                     route_profile="lit", use_llm=False))
        cell = h3.latlng_to_cell(51.0894, 71.4161, 10)
        ok_cell = client.post("/api/recommend", json=body(location={"h3_cell": cell},
                                                          prefs=ALL_PREFS,
                                                          exclude_poi_ids=EXCLUDE))
    assert ok.status_code == 200 and ok_cell.status_code == 200
    assert any(f.startswith("предпочтения:") for f in ok.json()["data_used"]["filters"])
    assert "POST /api/recommend -> 200" in caplog.text
    assert _pref_leaks(caplog.records) == []


@pytest.mark.parametrize("prefs", [
    {"likes": ["history"]},  # not a PrefTag (E18)
    {"likes": ["тихо, у воды, без людей"]},  # free text
    {"likes": ["quiet"], "history": ["coffee"]},  # an extra field
    {"avoids": ["no_bars"]},  # no_bars is a like
])
def test_bad_prefs_are_422_without_echo(client: TestClient, caplog: pytest.LogCaptureFixture,
                                        prefs: dict[str, object]) -> None:
    with caplog.at_level(logging.INFO):
        r = client.post("/api/recommend", json=body(prefs=prefs))
    assert r.status_code == 422
    for text in (r.text, caplog.text):
        assert "history" not in text and "у воды" not in text and "no_bars" not in text


@pytest.mark.parametrize("prefs", [
    {"likes": ["kids"], "avoids": ["kids_places"]},  # 1.3.1 conflicting pairs
    {"likes": ["coffee"], "avoids": ["cafes"]},
    {"likes": ["bars"]},  # a tag of the other list
])
def test_conflicting_or_foreign_prefs_are_422(client: TestClient, prefs: dict[str, object]
                                              ) -> None:
    r = client.post("/api/recommend", json=body(prefs=prefs))
    assert r.status_code == 422
    assert "kids_places" not in r.text and "cafes" not in r.text and "bars" not in r.text


def test_a_weather_warm_up_that_cannot_start_never_stops_the_server(
    deps: Deps, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_thread(_deps: Deps) -> None:
        raise RuntimeError("can't start new thread")

    monkeypatch.setattr(api, "build_default_deps", lambda: deps)
    monkeypatch.setattr(api, "start_weather_warm_up", no_thread)
    with TestClient(api.create_app()) as c:
        assert c.get("/api/health").json()["poi_count"] == 14  # rule 5: the demo still runs
