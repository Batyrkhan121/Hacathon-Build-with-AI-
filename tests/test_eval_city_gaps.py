"""City gaps map checks (wave 9, T144). No network, no app: the mock file and fakes."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from eval import city_gaps_check as cg

MOCK = cg.ROOT / "web" / "mock" / "city_gaps.json"


@pytest.fixture()
def raw() -> dict:
    return json.loads(MOCK.read_text(encoding="utf-8"))


def _totals(city: dict, report: dict | None = None) -> cg.LocalTotals:
    """Local totals that agree with this city block exactly."""
    share = city["walk_lit_unknown_m"] / city["walk_m"]
    return cg.LocalTotals(
        poi=city["poi"], poi_no_hours=city["poi_no_hours"],
        poi_wheelchair_unknown=city["poi_wheelchair_unknown"],
        poi_no_name_kk=city["poi_no_name_kk"], length_km=city["walk_m"] / 1000,
        lit_unknown_share=share, crossings=city["crossings"],
        crossings_kind_unknown=city["crossings_kind_unknown"],
        crossings_kerb_unknown=city["crossings_kerb_unknown"],
        report=report if report is not None else {
            "lit_unknown_pct": round(100 * share, 1),
            "crossings_kind_unknown": city["crossings_kind_unknown"],
            "crossings_kerb_unknown": city["crossings_kerb_unknown"],
            "poi_wheelchair_unknown": city["poi_wheelchair_unknown"]})


def test_mock_passes_the_shape_checks(raw: dict) -> None:
    gaps, checks = cg.check_shape(raw)
    assert gaps is not None
    assert [c.id for c in checks] == ["schema", "attribution", "cells_r8", "city_sum"]
    assert all(c.passed for c in checks), checks


def test_broken_file_fails_the_schema(raw: dict) -> None:
    raw["city"]["poi"] += 1  # no longer the sum over cells
    gaps, checks = cg.check_shape(raw)
    assert gaps is None and not checks[0].passed
    assert cg.check_shape({"cells": [], "user_query": "кофе"})[0] is None  # extra field


def test_reconcile_exact_and_tolerances(raw: dict) -> None:
    gaps, _ = cg.check_shape(raw)
    assert gaps is not None
    city = raw["city"]
    checks = cg.check_reconcile(gaps, _totals(city))
    assert all(c.passed for c in checks), [c for c in checks if not c.passed]
    off = _totals(city)
    off.crossings_kind_unknown += 1  # exact counters: one off is a failure
    off.length_km += 0.6  # beyond 0.5 km
    off.lit_unknown_share += 0.0015  # 0.15 p.p.: beyond 0.1
    failed = {c.id for c in cg.check_reconcile(gaps, off) if not c.passed}
    assert failed == {"crossings_kind_unknown", "walk_km", "lit_unknown_share"}
    near = _totals(city)
    near.length_km += 0.4
    near.lit_unknown_share += 0.0009
    assert all(c.passed for c in cg.check_reconcile(gaps, near))


def test_reconcile_needs_the_report_lines(raw: dict) -> None:
    gaps, _ = cg.check_shape(raw)
    assert gaps is not None
    checks = cg.check_reconcile(gaps, _totals(raw["city"], report={}))
    missing = [c for c in checks if c.id.startswith("report_")]
    assert len(missing) == 4 and all("строки нет" in c.detail for c in missing)


def test_report_numbers_are_read_as_the_report_prints_them() -> None:
    text = (
        "| unknown | 1155 | 94.5% |\n"
        "| нет данных об освещении | 5372.7 | 97.1% | 82842 | 96.4% |\n"
        "| тротуары и дорожки: нет данных об освещении | 1185.6 | 94.4% | 1 | 1% |\n"
        "| тип не указан | 1385 | 34.0% |\n"
        "| бордюр: нет данных | 4004 | 98.3% |\n")
    assert cg._report_numbers(text) == {
        "lit_unknown_pct": 97.1, "crossings_kind_unknown": 1385,
        "crossings_kerb_unknown": 4004, "poi_wheelchair_unknown": 1155}


def test_local_totals_follow_the_architecture_rules(tmp_path: Path) -> None:
    pois = [
        {"id": "a", "opening_hours": "Mo-Su 10:00-22:00", "wheelchair": "yes",
         "name_i18n": {"kk": "Атауы"}},
        {"id": "b", "opening_hours": "", "wheelchair": "unknown", "name_i18n": {"kk": " "}},
        {"id": "c", "name_i18n": {}},
    ]
    (tmp_path / "poi.jsonl").write_text(
        "\n".join(json.dumps(p, ensure_ascii=False) for p in pois) + "\n", encoding="utf-8")
    (tmp_path / "walk_coverage.json").write_text(json.dumps({
        "length_km": 12.5, "lit": {"unknown": {"length_share": 0.9}}, "crossings_total": 7,
        "crossings": {"unknown": {"nodes": 3}}, "kerbs": {"unknown": {"nodes": 6}}}),
        encoding="utf-8")
    (tmp_path / "REPORT.md").write_text("| тип не указан | 3 | 42.9% |\n", encoding="utf-8")
    got = cg.local_totals(tmp_path)
    assert (got.poi, got.poi_no_hours, got.poi_wheelchair_unknown, got.poi_no_name_kk) == (
        3, 2, 2, 2)  # «c» has no wheelchair field: unknown, as in POI
    assert (got.crossings, got.crossings_kind_unknown, got.crossings_kerb_unknown) == (7, 3, 6)
    assert got.report == {"crossings_kind_unknown": 3}


def _server(answer: str, echo: bool = False, vary: bool = False, status: int = 200,
            health: dict | None = None):
    """A fake API: /api/city-gaps answers `answer` with `status`; /api/health follows it."""
    def get(params: dict[str, str], path: str = cg.CITY_GAPS_PATH):
        if path == cg.HEALTH_PATH:
            body = health if health is not None else {
                "status": "ok", "city_gaps": "ready" if status == 200 else "missing"}
            return 200, json.dumps(body), {}
        text = answer
        if echo and params:
            text = answer[:-1] + f', "seen": "{params["query"]}"}}'
        if vary and params:
            text = json.dumps({"other": True})
        return status, text, {"cache-control": "public, max-age=3600"}
    return get


UNAVAILABLE = json.dumps({"detail": "city gaps unavailable"})


def test_privacy_params_ignored(raw: dict) -> None:
    text = json.dumps(raw, ensure_ascii=False)
    checks = {c.id: c for c in cg.check_privacy(_server(text))}
    assert checks["privacy_params"].passed and checks["cache_control"].passed
    assert "privacy_log" not in checks  # --url: no logs
    echoed = {c.id: c for c in cg.check_privacy(_server(text, echo=True))}
    assert not echoed["privacy_params"].passed
    varied = {c.id: c for c in cg.check_privacy(_server(text, vary=True))}
    assert not varied["privacy_params"].passed
    down = cg.check_privacy(_server(text, status=503))
    assert len(down) == 1 and not down[0].passed and "503" in down[0].detail


def test_privacy_log_counts_without_copying(raw: dict) -> None:
    text = json.dumps(raw, ensure_ascii=False)
    lines = ["GET /api/city-gaps -> 200 in 2 ms", "query=кофе рядом с домом lat=51.0894"]
    checks = {c.id: c for c in cg.check_privacy(_server(text), lambda: lines)}
    log = checks["privacy_log"]
    assert not log.passed and "с параметрами 1" in log.detail
    assert "кофе" not in log.detail and "51.0894" not in log.detail
    clean = {c.id: c for c in cg.check_privacy(_server(text), lambda: lines[:1])}
    assert clean["privacy_log"].passed


def test_run_checks_endpoint_states(raw: dict) -> None:
    text = json.dumps(raw, ensure_ascii=False)
    [missing] = cg.run_checks(_server("", status=404), None, reconcile=False)[0]
    assert not missing.passed and "T142" in missing.detail
    [silent] = cg.run_checks(_server("сеть: ConnectError", status=-1), None, reconcile=False)[0]
    assert not silent.passed and "сервер не ответил" in silent.detail
    down = {c.id: c for c in cg.run_checks(_server(UNAVAILABLE, status=503), None,
                                           reconcile=False)[0]}
    assert down["health_field"].passed and down["unavailable_body"].passed
    assert not down["endpoint"].passed and "503" in down["endpoint"].detail
    checks, notes = cg.run_checks(_server(text), None, reconcile=False)
    assert all(c.passed for c in checks), checks
    assert "health_field" in {c.id for c in checks}
    assert any("--local-data-check" in n for n in notes)  # --url: not reconciled


def test_health_and_503_body(raw: dict) -> None:
    text = json.dumps(raw, ensure_ascii=False)
    wrong = cg.check_health(_server(text, health={"status": "ok", "city_gaps": "missing"}))
    assert not wrong[0].passed  # the map answers 200, health says missing
    no_field = cg.check_health(_server(text, health={"status": "ok"}))
    assert not no_field[0].passed
    chatty = {c.id: c for c in cg.check_health(
        _server('{"detail": "city gaps unavailable: FileNotFoundError data/x.json"}', status=503))}
    assert not chatty["unavailable_body"].passed  # the 503 tells no details


def test_missing_file_keeps_the_rest_of_the_api(raw: dict) -> None:
    text = json.dumps(raw, ensure_ascii=False)
    with_file = _server(text)
    ok = cg.check_missing_file(with_file, _server(UNAVAILABLE, status=503))
    assert all(c.passed for c in ok), ok
    degraded = _server(UNAVAILABLE, status=503,
                       health={"status": "degraded", "city_gaps": "missing"})
    changed = {c.id: c for c in cg.check_missing_file(with_file, degraded)}
    assert not changed["missing_health"].passed  # the overall status must not change
    still_200 = {c.id: c for c in cg.check_missing_file(with_file, _server(text))}
    assert not still_200["missing_503"].passed


def test_privacy_log_finds_encoded_values(raw: dict) -> None:
    from urllib.parse import quote, quote_plus

    text = json.dumps(raw, ensure_ascii=False)
    query = cg.PROBE_PARAMS["query"]
    forms = cg.probe_forms()
    assert quote(query, safe="") in forms and quote_plus(query) in forms
    for line in (f"GET /api/city-gaps?query={quote_plus(query)} -> 200",
                 f"raw {quote(query, safe='')}"):
        checks = {c.id: c for c in cg.check_privacy(_server(text), lambda line=line: [line])}
        assert not checks["privacy_log"].passed, line
    echoed = text[:-1] + f', "q": "{quote_plus(query)}"}}'
    checks = {c.id: c for c in cg.check_privacy(lambda params, path=cg.CITY_GAPS_PATH: (
        200, echoed if params else text, {}))}
    assert not checks["privacy_params"].passed


def test_cli_file_only_writes_report(tmp_path: Path) -> None:
    cg.main(["--file", str(MOCK), "--file-only", "--out", str(tmp_path)])
    report = json.loads((tmp_path / "report.city_gaps.json").read_text(encoding="utf-8"))
    by_id = {c["id"]: c for c in report["checks"]}
    assert by_id["schema"]["passed"] and by_id["city_sum"]["passed"]
    assert not by_id["poi"]["passed"]  # the mock's numbers are made up: reconciliation fails
    assert "privacy_params" not in by_id
    md = (tmp_path / "report.city_gaps.md").read_text(encoding="utf-8")
    assert "Карта пробелов" in md


def test_inprocess_app_does_not_warm_up_the_weather(monkeypatch, tmp_path: Path) -> None:
    """In-process eval must not reach Open-Meteo: the lifespan warm-up thread is never made.
    Threads are recorded when they are created (synchronously), so there is no race with a
    background thread that may or may not have run yet."""
    import threading

    import app.api

    made: list[str] = []

    class SpyThread(threading.Thread):
        def __init__(self, *args, **kwargs) -> None:
            made.append(kwargs.get("name") or "")
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(app.api.threading, "Thread", SpyThread)
    get, logs, close = cg.inprocess_get(tmp_path / "no_city_gaps.json")
    try:
        status, _, _ = get({}, path=cg.HEALTH_PATH)
    finally:
        close()
    assert status in (200, 503)
    assert "weather-warm-up" not in made


def test_run_inprocess_closes_the_first_client_when_the_second_fails(monkeypatch) -> None:
    closed: list[str] = []
    calls: list[int] = []

    def fake(map_path=None):
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError("second app failed to start")
        return (lambda params, path=cg.CITY_GAPS_PATH: (200, "{}", {})), list, \
            (lambda: closed.append("first"))

    monkeypatch.setattr(cg, "inprocess_get", fake)
    with pytest.raises(RuntimeError):
        cg.run_inprocess()
    assert closed == ["first"]


# ------------------------------------------------------------ districts (1.4.1)

REAL = cg.ROOT / "data" / "processed" / "city_gaps.json"


def _sums_hold(raw: dict) -> None:
    gaps, _ = cg.check_shape(raw)
    assert gaps is not None and gaps.districts is not None
    sums, cells = cg.district_sums(gaps)
    assert sums == gaps.city.model_dump()  # all nine counters
    assert cells == len(gaps.cells)


def test_mock_districts_add_up_to_the_city(raw: dict) -> None:
    _sums_hold(raw)


def test_real_districts_add_up_to_the_city() -> None:
    raw = json.loads(REAL.read_text(encoding="utf-8"))
    if "districts" not in raw:
        pytest.skip("city_gaps.json без районов (файл 1.4.0): районы — DATA T151")
    _sums_hold(raw)


def test_mock_district_checks_pass(raw: dict) -> None:
    gaps, _ = cg.check_shape(raw)
    assert gaps is not None
    checks = {c.id: c for c in cg.check_districts(gaps)}
    assert all(c.passed for c in checks.values()), [c for c in checks.values() if not c.passed]
    assert checks["districts_status"].detail == "ok 1, stale 1, missing 1"


def test_district_checks_catch_errors(raw: dict) -> None:
    import copy

    from app.schemas import CityGaps

    gaps, _ = cg.check_shape(raw)
    assert gaps is not None
    # model_construct: what the schema would refuse, to see that eval refuses it on its own
    bad = copy.deepcopy(gaps)
    items = bad.districts.items
    missing = next(d for d in items if d.status == "missing")
    ok = next(d for d in items if d.status == "ok")
    object.__setattr__(missing, "counts", ok.counts)
    ring = list(ok.outline[0][:-1])
    object.__setattr__(ok, "outline", [ring])
    object.__setattr__(bad.districts, "unassigned_cells", bad.districts.unassigned_cells + 1)
    object.__setattr__(bad.districts, "official_list_checked", None)
    failed = {c.id for c in cg.check_districts(bad) if not c.passed}
    assert {"districts_status", "districts_outline", "districts_cells", "districts_official",
            "districts_cells_inside"} <= failed
    assert CityGaps  # the schema is the first line; eval is the second


def test_cell_in_the_wrong_district_is_caught(raw: dict) -> None:
    gaps, _ = cg.check_shape(raw)
    assert gaps is not None
    inside = next(c for c in gaps.cells if c.district is not None)
    object.__setattr__(inside, "district", None)
    checks = {c.id: c for c in cg.check_districts(gaps)}
    assert not checks["districts_cells_inside"].passed


def test_file_without_districts_is_reported(raw: dict) -> None:
    raw.pop("districts")
    for cell in raw["cells"]:
        cell.pop("district", None)
    gaps, _ = cg.check_shape(raw)
    assert gaps is not None
    [check] = cg.check_districts(gaps)
    assert not check.passed and "T151" in check.detail


def test_even_odd_with_a_hole() -> None:
    outer = [[0, 0], [0, 10], [10, 10], [10, 0], [0, 0]]
    hole = [[4, 4], [4, 6], [6, 6], [6, 4], [4, 4]]
    assert cg._in_outline(2, 2, [outer, hole])
    assert not cg._in_outline(5, 5, [outer, hole])
    assert not cg._in_outline(12, 5, [outer])
