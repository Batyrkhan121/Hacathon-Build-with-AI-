"""Smoke tests for the scenario eval (EVAL lane, T51). No network: --mock only."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.schemas import Answer, RecommendRequest
from eval import run_eval
from eval.run_eval import Outcome, check_expect, check_invariants, detect_script, grade


@pytest.fixture(scope="module")
def mock_answer() -> Answer:
    return Answer.model_validate(json.loads(run_eval.MOCK_PATH.read_text(encoding="utf-8")))


W8_IDS = {f"E{i}" for i in range(13, 19)}


def test_scenarios_are_22_requests() -> None:
    """16 from the submission (unchanged) + E13–E18 of wave 8; E18 must be a schema 422.
    E12 is the live case example in docs/SCENARIOS.md, not a graded scenario."""
    scenarios = run_eval.load_scenarios()
    ids = [s["id"] for s in scenarios]
    assert ids == ([f"S{i}" for i in range(1, 6)] + [f"E{i}" for i in range(1, 12)]
                   + [f"E{i}" for i in range(13, 19)])
    for s in scenarios:
        assert s["expect"], s["id"]
        assert s.get("wave") == (8 if s["id"] in W8_IDS else None), s["id"]
        if s["expect"].get("reject_required"):
            with pytest.raises(ValidationError):
                RecommendRequest.model_validate(s["request"])
        else:
            RecommendRequest.model_validate(s["request"])


def test_mock_mode_writes_reports(tmp_path: Path) -> None:
    code = run_eval.main(["--mock", "--out", str(tmp_path), "--note", "индекс устарел"])
    assert code == 0
    report = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert report["mode"] == "mock"
    assert report["total"] == 22
    assert {r["id"] for r in report["results"]} >= {"S1", "E10"}
    md = (tmp_path / "report.md").read_text(encoding="utf-8")
    assert "- индекс устарел" in md
    assert f"Итог: {report['passed']}/22" in md
    # the 16 of the submission stay comparable with README / PITCH / slides
    assert report["groups"]["core"]["total"] == 16
    assert report["groups"]["wave8"]["total"] == 6
    core, w8 = report["groups"]["core"], report["groups"]["wave8"]
    assert f"основные {core['passed']}/16, волна 8 {w8['passed']}/6" in md
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


# ------------------------------------------------------- walking route on the graph (1.1.0)


def graph_answer(mock_answer: Answer) -> Answer:
    """The mock route marked as graph: its path already goes origin -> stops."""
    data = mock_answer.model_dump()
    data["route"]["path_source"] = "graph"
    data["route"]["crossings"] = [{"lat": 51.09, "lon": 71.413, "kind": "signals"}]
    return Answer.model_validate(data)


def test_graph_route_consistent_passes(mock_answer: Answer) -> None:
    assert run_eval.check_graph_route(graph_answer(mock_answer)) == []


def test_straight_route_is_not_graph_checked(mock_answer: Answer) -> None:
    ans = mock_answer.model_copy(deep=True)
    ans.route.total_walk_m = 1  # would fail the graph checks
    assert run_eval.check_graph_route(ans) == []


def test_graph_route_far_start_and_wrong_total(mock_answer: Answer) -> None:
    ans = graph_answer(mock_answer)
    ans.route.path[0].lat += 0.002  # ≈220 m north of the start
    ans.route.total_walk_m += 100
    fails = run_eval.check_graph_route(ans)
    assert any("начинается" in f for f in fails)
    assert any("total_walk_m" in f for f in fails)


def test_graph_route_minutes_not_below_straight(mock_answer: Answer) -> None:
    ans = graph_answer(mock_answer)
    ans.route.legs[1].walk_min = 1  # 380 m straight needs at least 6 min at 75 m/min
    assert any("по прямой" in f for f in run_eval.check_graph_route(ans))


def test_graph_loop_route_ends_at_start(mock_answer: Answer) -> None:
    from app.schemas import RouteLeg

    ans = graph_answer(mock_answer)
    back = RouteLeg(from_id="mock:poi/mega_silk_way", to_id="origin", distance_m=157, walk_min=3)
    ans.route.legs.append(back)
    ans.route.total_walk_m += back.distance_m
    ans.route.path.append(ans.context.origin.model_copy())
    assert run_eval.check_graph_route(ans) == []


def test_report_counts_graph_routes(tmp_path: Path) -> None:
    run_eval.main(["--mock", "--out", str(tmp_path)])
    report = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert report["routes"] == {"graph": 0, "straight": 21}  # E18 is a 422, no answer
    assert "Маршруты: по графу тротуаров 0" in (tmp_path / "report.md").read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("straight_m", "minutes"), [(0, 0), (226, 3), (227, 3), (229, 4), (300, 4)]
)
def test_min_walk_minutes_has_tolerance(straight_m: float, minutes: int) -> None:
    """226 m straight is 3.01 min: within the 3 m tolerance a 3-minute graph leg is fine."""
    assert run_eval.min_walk_minutes(straight_m) == minutes


def test_route_source_expectation(mock_answer: Answer) -> None:
    assert check_expect(mock_answer, {"route_source": "straight"}) == []
    assert any("ожидали graph" in f for f in check_expect(mock_answer, {"route_source": "graph"}))
    no_route = mock_answer.model_copy(deep=True)
    no_route.route = None
    assert any("маршрут нет" in f for f in check_expect(no_route, {"route_source": "graph"}))


def test_graph_expectation_skipped_when_run_has_no_graph(mock_answer: Answer) -> None:
    """WALK_GRAPH=false: no answer in the run is a graph route -> route_source=graph is skipped;
    once any answer is a graph route, a straight one in a graph-expecting scenario fails."""
    base = run_eval.load_scenarios()[0]
    scns = [{**base, "id": "X1", "expect": {"route_source": "graph"}},
            {**base, "id": "X2", "expect": {}}]
    straight = run_eval.mock_runner()
    assert all(r.passed for r in run_eval.run_all(scns, straight))

    graph = graph_answer(mock_answer)
    answers = iter([mock_answer, graph])  # X1 straight, X2 graph

    def mixed(req: RecommendRequest) -> Outcome:
        return Outcome(answer=next(answers))

    results = {r.id: r for r in run_eval.run_all(scns, mixed)}
    assert not results["X1"].passed and results["X2"].passed


# ------------------------------------------------------------- privacy probes (1.2.0)


PROBE_IDS = ["r11", "raw_coords", "prefs_unknown", "prefs_free_text", "prefs_extra"]


def test_privacy_probes_schema_rejects_all() -> None:
    probes = run_eval.run_privacy_probes(None)
    assert [p["id"] for p in probes] == PROBE_IDS
    assert all(p["passed"] and p["got"] == "422" for p in probes), probes


def test_privacy_probes_over_http() -> None:
    """The server answer decides: 422 passes, 200 (cell coarsened / coords taken) fails."""
    sent: list[dict] = []

    def post(body: dict) -> int:
        sent.append(body)
        return 422 if "lat" in body["location"] else 200

    probes = {p["id"]: p for p in run_eval.run_privacy_probes(post)}
    assert not probes["r11"]["passed"] and probes["r11"]["got"] == "200"
    assert probes["raw_coords"]["passed"]
    assert not probes["prefs_unknown"]["passed"]
    assert len(sent) == 5
    assert "1/5 отклонены" in run_eval.privacy_summary(list(probes.values()))


def test_privacy_probe_fails_when_422_echoes_the_input() -> None:
    def post(body: dict) -> tuple[int, str]:
        prefs = body.get("prefs") or {}
        return 422, json.dumps({"detail": f"bad tag {prefs.get('likes')}"})

    probes = {p["id"]: p for p in run_eval.run_privacy_probes(post)}
    assert not probes["prefs_unknown"]["passed"]
    assert "повторяет ввод" in probes["prefs_unknown"]["got"]
    assert probes["r11"]["passed"]  # the fixed text does not contain the cell


def test_report_has_privacy_block(tmp_path: Path) -> None:
    run_eval.main(["--mock", "--out", str(tmp_path)])
    report = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert [p["id"] for p in report["privacy"]] == PROBE_IDS
    md = (tmp_path / "report.md").read_text(encoding="utf-8")
    assert "Приватность: 5/5 отклонены с 422" in md


# ------------------------------------------------------------ wave 8 (contract 1.3.0)


def _routed(ans: Answer, **fields) -> Answer:
    raw = ans.model_dump(mode="json")
    raw["route"].update(fields)
    return Answer.model_validate(raw)


def test_route_profile_and_facts_expectations(mock_answer: Answer) -> None:
    exp = {"route_profile": "accessible", "route_facts": True}
    fails = check_expect(mock_answer, exp)
    assert any("ожидали accessible" in f for f in fails)
    assert any("нет фактов 1.3.0" in f for f in fails)
    ok = _routed(mock_answer, profile="accessible", lit_share=0, unlit_share=0,
                 unknown_lit_share=1, kerb_lowered=0, kerb_unknown=0, max_outdoor_leg_min=7)
    assert check_expect(ok, exp) == []
    assert check_invariants(ok) == []


def test_route_facts_must_agree_with_route(mock_answer: Answer) -> None:
    bad = _routed(mock_answer, lit_share=0.5, unlit_share=0, unknown_lit_share=0.5,
                  max_outdoor_leg_min=30, kerb_lowered=1, kerb_unknown=0)
    fails = " ".join(check_invariants(bad))
    assert "ожидали 0/0/1" in fails  # straight segments carry no lit data
    assert "max_outdoor_leg_min 30" in fails
    assert "больше переходов" in fails  # a straight route has no crossings


def test_accessible_with_steps_needs_reason_and_lit_is_not_safety(mock_answer: Answer) -> None:
    bad = _routed(mock_answer, profile="accessible", steps_count=2,
                  note="Освещённый и безопасный путь")
    fails = " ".join(check_invariants(bad))
    assert "в note нет причины" in fails
    assert "как безопасность" in fails
    ok = _routed(mock_answer, profile="accessible", steps_count=2,
                 note="Без ступенек пути нет: на маршруте 2 лестницы")
    assert check_invariants(ok) == []


def test_warm_expectations(mock_answer: Answer) -> None:
    exp = {"max_outdoor_leg_min": 5, "indoor_cards_min": 3}
    fails = check_expect(_routed(mock_answer, max_outdoor_leg_min=7), exp)
    assert any("на улице подряд 7 мин" in f for f in fails)
    assert any("мест в помещении 2 < 3" in f for f in fails)  # the park is outdoors


def _event(ans: Answer, **fields) -> Answer:
    raw = ans.model_dump(mode="json")
    raw["cards"][0].update({"kind": "event", "facts_sources": ["manual"],
                            "source_url": "https://example.org/afisha"})
    raw["cards"][0].update(fields)
    raw["route"] = None
    return Answer.model_validate(raw)


def test_event_cards(mock_answer: Answer) -> None:
    # mock local_time is 20.10 15:30
    ok = _event(mock_answer, starts_at="2026-10-20T19:00:00+05:00")
    assert check_invariants(ok) == []
    assert check_expect(ok, {"event_required": True}) == []
    assert check_expect(mock_answer, {"event_required": True}) == ["нет карточки события"]
    past = _event(mock_answer, starts_at="2026-10-20T10:00:00+05:00",
                  ends_at="2026-10-20T12:00:00+05:00")
    assert any("уже закончилось" in f for f in check_invariants(past))
    yesterday = _event(mock_answer, starts_at="2026-10-19T19:00:00+05:00")
    assert any("раньше запроса" in f for f in check_invariants(yesterday))
    live = _event(mock_answer, starts_at="2026-10-20T19:00:00+05:00",
                  why="Концерт идёт сейчас, успеете.")
    assert any("идёт сейчас" in f for f in check_invariants(live))
    on_now = _event(mock_answer, starts_at="2026-10-20T15:00:00+05:00",
                    ends_at="2026-10-20T17:00:00+05:00", why="Выставка идёт сейчас.")
    assert check_invariants(on_now) == []
    no_manual = _event(mock_answer, starts_at="2026-10-20T19:00:00+05:00",
                       facts_sources=["osm"])
    assert any("ожидали manual" in f for f in check_invariants(no_manual))


def test_prefs_shift(mock_answer: Answer) -> None:
    spec = {"likes_categories": ["museum", "gallery"], "avoid_categories": ["mall"]}
    base = mock_answer  # museum, park, mall: score 0
    better = mock_answer.model_copy(deep=True)
    better.cards[2].category = "gallery"  # mall gone: score 2
    assert run_eval.check_prefs_shift(better, base, spec) == []
    assert "не сдвинули" in run_eval.check_prefs_shift(base, base, spec)[0]
    assert run_eval.check_prefs_shift(better, None, spec) == ["нет ответа без prefs для сравнения"]
    all_liked = {"likes_categories": ["museum", "park", "mall"]}
    assert "не различает" in run_eval.check_prefs_shift(base, base, all_liked)[0]


def test_run_all_sends_baseline_without_prefs(mock_answer: Answer) -> None:
    seen: list[RecommendRequest] = []

    def runner(req: RecommendRequest) -> Outcome:
        seen.append(req)
        return Outcome(answer=mock_answer)

    scn = {"id": "X", "title": "x", "request": {
        "query": "гулять", "location": {"landmark_id": "expo"},
        "prefs": {"likes": ["museums"]}},
        "expect": {"prefs_shift": {"likes_categories": ["gallery"]}}}
    [res] = run_eval.run_all([scn], runner)
    assert [r.prefs is not None for r in seen] == [True, False]
    assert any("не сдвинули" in f for f in res.failures)


def test_prefs_in_log_with_place_is_caught() -> None:
    req = {"query": "гулять рядом", "location": {"landmark_id": "expo"},
           "prefs": {"likes": ["museums"], "avoids": ["coffee"]}}
    ok = ["recommend id=ab12 res=lm cards=3 ms=40", "prefs: likes=1 avoids=1"]
    assert run_eval.check_prefs_not_logged(ok, req) == []
    assert run_eval.check_prefs_not_logged(["recommend landmark=expo prefs=museums"], req)
    assert run_eval.check_prefs_not_logged(["query='гулять рядом' tags=coffee"], req)
    assert run_eval.check_prefs_not_logged(None, req) == []  # --url: logs not visible


def test_capture_logs_sees_info_records() -> None:
    import logging

    with run_eval.capture_logs() as lines:
        logging.getLogger("qadam.test").info("recommend id=%s", "x1")
    assert lines == ["recommend id=x1"]


def test_reject_required_scenario(mock_answer: Answer) -> None:
    scn = {"id": "X", "title": "x", "request": {
        "query": "кофе", "location": {"landmark_id": "expo"}, "prefs": {"likes": ["history"]}},
        "expect": {"reject_required": True, "reject_body_forbid": ["history"]}}

    def answer(req: RecommendRequest) -> Outcome:
        return Outcome(answer=mock_answer)

    [res] = run_eval.run_all([scn], answer)
    assert res.passed, res.failures  # in-process: the schema rejection is the 422
    sent: list[dict] = []

    def raw_post(body: dict) -> tuple[int, str]:
        sent.append(body)
        return 422, '{"detail":[{"msg":"Неверный формат запроса."}]}'

    [res] = run_eval.run_all([scn], answer, raw_post=raw_post)
    assert res.passed and sent[0]["prefs"] == {"likes": ["history"]}
    [res] = run_eval.run_all([scn], answer, raw_post=lambda body: (422, "unknown tag history"))
    assert any("повторяет ввод" in f for f in res.failures)
    [res] = run_eval.run_all([scn], answer, raw_post=lambda body: (200, "{}"))
    assert not res.passed
    answered = grade(scn, Outcome(answer=mock_answer))
    assert "ожидали 422, а пришёл ответ" in answered.failures
