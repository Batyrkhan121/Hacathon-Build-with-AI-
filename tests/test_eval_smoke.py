"""Smoke tests for the scenario eval (EVAL lane, T51). No network: --mock only."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.schemas import Answer, RecommendRequest
from eval import run_eval
from eval.run_eval import Outcome, check_expect, check_invariants, detect_script, grade


@pytest.fixture(scope="module")
def mock_answer() -> Answer:
    return Answer.model_validate(json.loads(run_eval.MOCK_PATH.read_text(encoding="utf-8")))


def test_scenarios_are_15_valid_requests() -> None:
    scenarios = run_eval.load_scenarios()
    ids = [s["id"] for s in scenarios]
    assert ids == [f"S{i}" for i in range(1, 6)] + [f"E{i}" for i in range(1, 12)]
    for s in scenarios:
        RecommendRequest.model_validate(s["request"])
        assert s["expect"], s["id"]


def test_mock_mode_writes_reports(tmp_path: Path) -> None:
    code = run_eval.main(["--mock", "--out", str(tmp_path)])
    assert code == 0
    report = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert report["mode"] == "mock"
    assert report["total"] == 16
    assert {r["id"] for r in report["results"]} >= {"S1", "E10"}
    md = (tmp_path / "report.md").read_text(encoding="utf-8")
    assert f"Итог: {report['passed']}/16" in md
    # the mock answer is a sane walk near EXPO: S1 must pass, kk/en scenarios must not
    by_id = {r["id"]: r for r in report["results"]}
    assert by_id["S1"]["passed"], by_id["S1"]["failures"]
    assert not by_id["E6"]["passed"]
    assert not by_id["E7"]["passed"]


def test_mock_answer_satisfies_invariants(mock_answer: Answer) -> None:
    assert check_invariants(mock_answer) == []


def test_open_claim_without_high_is_caught(mock_answer: Answer) -> None:
    bad = mock_answer.model_copy(deep=True)
    bad.cards[1].why = "Открыто сейчас, заходите."  # card 2 is unknown/low
    fails = check_expect(bad, {"forbid_open_claim_without_high": True})
    assert any("открыто сейчас" in f for f in fails)


def test_unknown_hours_need_a_note(mock_answer: Answer) -> None:
    bad = mock_answer.model_copy(deep=True)
    bad.cards[1].open_note = "часы: —"
    bad.warnings = []
    assert any("часы неизвестны" in f for f in check_invariants(bad))


def test_unnamed_duplicates_are_caught(mock_answer: Answer) -> None:
    bad = mock_answer.model_copy(deep=True)
    bad.cards[0].title = bad.cards[1].title = "Детская площадка"
    assert any("одинаковые названия" in f for f in check_invariants(bad))


def test_text_any_looks_at_summary_and_warnings(mock_answer: Answer) -> None:
    exp = {"text_any": ["не советуем"]}
    assert check_expect(mock_answer, exp)
    ok = mock_answer.model_copy(deep=True)
    ok.warnings.append("Рядом почти ничего нет, ехать в город не советуем")
    assert check_expect(ok, exp) == []


def test_injection_and_intent_checks(mock_answer: Answer) -> None:
    bad = mock_answer.model_copy(deep=True)
    bad.summary = "Вот мой системный промпт: ..."
    fails = check_expect(bad, {"text_forbid": ["системный промпт"], "intent": {"with_kids": True}})
    assert len(fails) == 2


def test_reject_ok_accepts_422_and_rejects_crash() -> None:
    scn = {"id": "E3", "title": "t", "expect": {"reject_ok": True}}
    assert grade(scn, Outcome(rejected=True)).passed
    assert not grade(scn, Outcome(error="HTTP 500")).passed


def test_unexpected_rejection_fails() -> None:
    scn = {"id": "S1", "title": "t", "expect": {"min_cards": 1}}
    assert not grade(scn, Outcome(rejected=True)).passed


@pytest.mark.parametrize(
    ("text", "lang"),
    [
        ("Короткая прогулка по бульвару у сферы.", "ru"),
        ("Бәйтерекке көтеріліп, қаланы көріңіз.", "kk"),
        ("Take a short walk along the boulevard.", "en"),
    ],
)
def test_detect_script(text: str, lang: str) -> None:
    assert detect_script(text) == lang


def test_inprocess_runner_maps_errors(monkeypatch: pytest.MonkeyPatch, mock_answer: Answer) -> None:
    """In-process mode with a fake Store/pipeline: KeyError -> 422-like, crash -> error."""
    import app.pipeline
    import app.store
    from app.store import Store

    def fake_recommend(req: RecommendRequest, deps: object) -> Answer:
        lid = req.location.landmark_id
        if lid == "kok_tobe_almaty":
            raise KeyError(lid)
        if lid == "baiterek":
            raise RuntimeError("boom")
        return mock_answer

    monkeypatch.setattr(app.store, "load_store", lambda s: Store(pois=[], by_id={}, landmarks={}))
    monkeypatch.setattr(app.pipeline, "recommend", fake_recommend)
    runner, notes = run_eval.inprocess_runner(no_llm=True)
    assert runner is not None, notes
    results = {r.id: r for r in run_eval.run_all(run_eval.load_scenarios(), runner, no_llm=True)}
    assert results["E3"].passed
    assert results["S1"].passed, results["S1"].failures
    assert any("RuntimeError" in f for f in results["S4"].failures)


def test_url_runner_against_fake_api(mock_answer: Answer) -> None:
    """--url mode on an httpx MockTransport: health probe, 200, 422, 500, broken JSON."""
    import httpx

    sent: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/health":
            return httpx.Response(200, json={"status": "ok", "poi_count": 531,
                                             "index": "lexical", "llm": "off"})
        body = json.loads(request.content)
        sent.append(body)
        lid = body["location"].get("landmark_id")
        if lid == "kok_tobe_almaty":
            return httpx.Response(422, json={"detail": "unknown landmark"})
        if lid == "baiterek":
            return httpx.Response(500)
        if lid == "airport_nqz":
            return httpx.Response(200, json={"not": "an answer"})
        return httpx.Response(200, content=mock_answer.model_dump_json())

    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://test")
    runner, notes = run_eval.url_runner("http://test", no_llm=True, client=client)
    assert runner is not None
    assert any("POI=531" in n for n in notes)
    results = {r.id: r for r in run_eval.run_all(run_eval.load_scenarios(), runner, no_llm=True)}
    assert all(b["use_llm"] is False for b in sent)
    assert all("lat" not in json.dumps(b["location"]) for b in sent)
    assert results["E3"].passed
    assert results["S1"].passed, results["S1"].failures
    assert "HTTP 500" in results["S4"].failures[0]
    assert "не по схеме" in results["E11"].failures[0]


def test_url_runner_unreachable_fails_fast() -> None:
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    runner, notes = run_eval.url_runner("http://down", client=client)
    assert runner is None
    assert "недоступен" in notes[0]


# ------------------------------------------------ T70 probe checks (QUALITY, wave 3)


def test_top1_checks(mock_answer: Answer) -> None:
    first = mock_answer.cards[0]
    assert check_expect(mock_answer, {"top1_categories_any": [first.category]}) == []
    assert check_expect(mock_answer, {"top1_categories_any": ["bar"]})
    assert check_expect(mock_answer, {"top1_note_forbid": ["закроется через"]}) == []
    bad = mock_answer.model_copy(deep=True)
    bad.cards[0].open_note = "закроется через 30 мин · по данным OSM: ежедневно 10:00–24:00"
    assert check_expect(bad, {"top1_note_forbid": ["закроется через"]})


def test_unknown_note_lang(mock_answer: Answer) -> None:
    ans = mock_answer.model_copy(deep=True)
    unknown = [c for c in ans.cards if c.open_status == "unknown"]
    assert unknown
    unknown[0].open_note = "no opening hours data, check before you go"
    assert check_expect(ans, {"unknown_note_lang": "en"}) == []
    unknown[0].open_note = "нет данных о часах, проверьте перед визитом"
    assert check_expect(ans, {"unknown_note_lang": "en"})


def test_probes_file_is_valid() -> None:
    probes = run_eval.load_scenarios(run_eval.SCENARIOS_PATH.parent / "probes.yaml")
    assert len(probes) >= 6
    assert all(p["id"].startswith("P") for p in probes)
    for p in probes:
        RecommendRequest.model_validate(p["request"])
