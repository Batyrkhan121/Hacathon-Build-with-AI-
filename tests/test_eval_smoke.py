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


def test_scenarios_are_16_valid_requests() -> None:
    scenarios = run_eval.load_scenarios()
    ids = [s["id"] for s in scenarios]
    assert ids == [f"S{i}" for i in range(1, 6)] + [f"E{i}" for i in range(1, 12)]
    for s in scenarios:
        RecommendRequest.model_validate(s["request"])
        assert s["expect"], s["id"]


def test_mock_mode_writes_reports(tmp_path: Path) -> None:
    code = run_eval.main(["--mock", "--out", str(tmp_path), "--note", "индекс устарел"])
    assert code == 0
    report = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert report["mode"] == "mock"
    assert report["total"] == 16
    assert {r["id"] for r in report["results"]} >= {"S1", "E10"}
    md = (tmp_path / "report.md").read_text(encoding="utf-8")
    assert "- индекс устарел" in md
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


# ------------------------------------------------------------------ --smoke mode

SMOKE_CSV = """\
id,query,location,at,lang
# comment lines are skipped
Q1,"Хочу тихое место с кофе, розетка обязательна",expo,,
Q2,Куда сходить с ребёнком?,892153833a7ffff,2026-10-19T12:00:00+05:00,ru
Q3,Where can I get coffee at night?,,,en
Q4,Неизвестное место,Bad Id!,,
"""


def write_queries(tmp_path: Path, text: str = SMOKE_CSV, encoding: str = "utf-8") -> Path:
    path = tmp_path / "queries_team.csv"
    path.write_text(text, encoding=encoding)
    return path


def test_load_queries_skips_comments_and_bom(tmp_path: Path) -> None:
    rows = run_eval.load_queries(write_queries(tmp_path, encoding="utf-8-sig"))
    assert [r["id"] for r in rows] == ["Q1", "Q2", "Q3", "Q4"]
    assert rows[0]["query"] == "Хочу тихое место с кофе, розетка обязательна"


def test_smoke_request_defaults() -> None:
    req = run_eval.smoke_request({"id": "Q", "query": "кофе"}, no_llm=True)
    assert req.location.landmark_id == run_eval.SMOKE_DEFAULT_LOCATION
    assert req.use_llm is False
    assert req.at is not None and req.at.utcoffset() is not None
    cell = run_eval.smoke_request({"query": "кофе", "location": "892153833a7ffff"}, no_llm=False)
    assert cell.location.h3_cell == "892153833a7ffff"
    assert cell.use_llm is True


def test_smoke_mode_with_mock_writes_readable_report(tmp_path: Path, mock_answer: Answer) -> None:
    queries = write_queries(tmp_path)
    code = run_eval.main(["--smoke", "--mock", "--queries", str(queries), "--out", str(tmp_path)])
    assert code == 0
    md = (tmp_path / "smoke_report.md").read_text(encoding="utf-8")
    assert "Хочу тихое место с кофе, розетка обязательна" in md
    assert "Where can I get coffee at night?" in md
    for card in mock_answer.cards:
        assert card.title in md
        assert card.why in md
    assert "Bad Id!" in md  # invalid row is listed with its error, not dropped
    assert "не по схеме" in md
    assert "Ответили: 3/4" in md
    assert not (tmp_path / "report.md").exists()  # smoke never overwrites the strict report


def test_smoke_mode_without_pipeline_reports_every_query(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_pipeline(no_llm: bool) -> tuple[None, list[str]]:
        return None, ["pipeline ещё не готов"]

    monkeypatch.setattr(run_eval, "inprocess_runner", no_pipeline)
    queries = write_queries(tmp_path)
    run_eval.main(["--smoke", "--no-llm", "--queries", str(queries), "--out", str(tmp_path)])
    md = (tmp_path / "smoke_report.md").read_text(encoding="utf-8")
    assert "Ответили: 0/4" in md
    assert md.count("pipeline ещё не готов") >= 4


def test_default_queries_csv_is_valid() -> None:
    """--smoke without --queries uses eval/queries_team.csv: it must exist and parse."""
    rows = run_eval.load_queries()
    assert rows
    for row in rows:
        run_eval.smoke_request(row, no_llm=True)


def test_smoke_without_queries_file_does_not_crash(tmp_path: Path, capsys) -> None:
    code = run_eval.main(["--smoke", "--mock", "--queries", str(tmp_path / "nope.csv"),
                          "--out", str(tmp_path)])
    assert code == 0
    assert "нет файла запросов" in capsys.readouterr().out


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


def test_top1_why_any(mock_answer: Answer) -> None:
    word = mock_answer.cards[0].why.split()[0]
    assert check_expect(mock_answer, {"top1_why_any": [word]}) == []
    assert check_expect(mock_answer, {"top1_why_any": ["нет данных"]})


def test_notes_lang(mock_answer: Answer) -> None:
    ans = mock_answer.model_copy(deep=True)
    for c in ans.cards:
        c.open_note = "OSM hours: daily 10:00–22:00"
    assert check_expect(ans, {"notes_lang": "en"}) == []
    ans.cards[0].open_note = "по данным OSM: ежедневно 10:00–22:00"
    assert check_expect(ans, {"notes_lang": "en"})


@pytest.mark.parametrize(
    ("line", "mode"),
    [
        ("поиск: по смыслу и по словам", "vector"),
        ("поиск: по словам", "words"),
        ("search: by meaning and by words", "vector"),
        ("іздеу: сөздер бойынша", "words"),
    ],
)
def test_search_mode_from_filters(mock_answer: Answer, line: str, mode: str) -> None:
    ans = mock_answer.model_copy(deep=True)
    ans.data_used.filters = ["пешком ≤ 15 мин", line]
    assert run_eval.search_mode(Outcome(answer=ans)) == mode
    assert run_eval.search_line(Outcome(answer=ans)) == line
    assert run_eval.search_mode(Outcome(answer=mock_answer)) is None  # no such line -> unknown


def test_inprocess_counts_query_vectors(
    monkeypatch: pytest.MonkeyPatch, mock_answer: Answer
) -> None:
    """The runner wraps embed_query: a vector for S1, None (quota) for the rest."""
    import app.config
    import app.llm
    import app.pipeline
    import app.store
    from app.store import Store

    class FakeSettings:
        has_llm = True

    class FakeLLM:
        def __init__(self, settings: object) -> None:
            pass

        def embed_query(self, text: str) -> list[float] | None:
            return [1.0] if "Mega" in text else None

    def fake_recommend(req: RecommendRequest, deps: object) -> Answer:
        deps.llm.embed_query(req.query)  # type: ignore[attr-defined]
        return mock_answer

    monkeypatch.setattr(app.config, "get_settings", lambda: FakeSettings())
    monkeypatch.setattr(app.llm, "LLMClient", FakeLLM)
    monkeypatch.setattr(app.store, "load_store", lambda s: Store(pois=[], by_id={}, landmarks={}))
    monkeypatch.setattr(app.pipeline, "recommend", fake_recommend)
    runner, _ = run_eval.inprocess_runner(no_llm=False)
    assert runner is not None
    results = run_eval.run_all(run_eval.load_scenarios()[:2], runner)
    assert [r.search for r in results] == ["vector", "words"]
    summary = run_eval.search_summary(results)
    assert "вектор запроса (embed_query) у 1, только по словам 1" in summary


# ---------------------------------------- RAG review R4: eval uses the validate guard


@pytest.mark.parametrize(
    "claim", ["Все места открыты сейчас.", "В данный момент работает.", "Қазір ашық.", "Open 24/7."]
)
def test_open_claim_check_uses_shared_guard(mock_answer: Answer, claim: str) -> None:
    bad = mock_answer.model_copy(deep=True)
    unknown = next(c for c in bad.cards if c.open_status == "unknown")
    unknown.why = claim
    assert check_expect(bad, {"forbid_open_claim_without_high": True})


def test_open_claim_in_summary_is_caught(mock_answer: Answer) -> None:
    bad = mock_answer.model_copy(deep=True)
    bad.summary = "Все места открыты сейчас."
    assert any("summary" in f for f in check_expect(bad, {"forbid_open_claim_without_high": True}))
    all_open = bad.model_copy(deep=True)
    for c in all_open.cards:
        c.open_status, c.open_confidence = "open", "high"
    assert check_expect(all_open, {"forbid_open_claim_without_high": True}) == []


def test_24_7_template_reason_is_not_a_failed_claim(mock_answer: Answer) -> None:
    ans = mock_answer.model_copy(deep=True)
    card = next(c for c in ans.cards if c.open_status == "unknown")
    card.why = "≈20 мин пешком, круглосуточно."
    card.tags = [*card.tags, "24_7"]
    assert check_expect(ans, {"forbid_open_claim_without_high": True}) == []
    card.tags = [t for t in card.tags if t != "24_7"]
    assert check_expect(ans, {"forbid_open_claim_without_high": True})


def test_url_runner_notes_llm_off_with_reason() -> None:
    """Health now says "off (reason)": the note about template answers must still appear."""
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "ok", "poi_count": 1222,
                                         "index": "ready", "llm": "off (disabled)"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    runner, notes = run_eval.url_runner("http://test", client=client)
    assert runner is not None
    assert any("LLM на сервере выключена" in n for n in notes)


def test_url_runner_503_without_data_stops_with_reason() -> None:
    """Health 503 with a JSON body (0 POI): stop the run and say why, not 'unreachable'."""
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"status": "degraded", "poi_count": 0,
                                         "warnings": ["poi.jsonl не найден"]})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    runner, notes = run_eval.url_runner("http://test", client=client)
    assert runner is None
    assert "без данных" in notes[0]
    assert "status=degraded" in notes[0] and "POI=0" in notes[0]
    assert "poi.jsonl не найден" in notes[0]


def test_intent_categories_include_exclude(mock_answer: Answer) -> None:
    ans = mock_answer.model_copy(deep=True)
    ans.intent.categories = ["attraction", "park"]
    assert check_expect(ans, {"intent_categories_include": ["park"]}) == []
    assert check_expect(ans, {"intent_categories_exclude": ["park"]})
    ans.intent.categories = ["attraction"]
    assert check_expect(ans, {"intent_categories_include": ["park"]})
    assert check_expect(ans, {"intent_categories_exclude": ["park"]}) == []
