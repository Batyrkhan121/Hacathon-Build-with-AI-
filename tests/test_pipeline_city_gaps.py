"""pipeline.city_gaps: the «Карта пробелов» counters on a tiny synthetic graph (no network)."""

from __future__ import annotations

import json
import random
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import h3
import numpy as np
import pytest

from app.hours import ASTANA_TZ
from app.schemas import POI, CityGaps, GapCounts
from pipeline.city_gaps import (
    OUTSIDE,
    District,
    build_city_gaps,
    district_names,
    districts_from,
    districts_query,
    dump,
    inside_mask,
    newest_snapshot,
    reconcile,
    tally,
)
from pipeline.normalize import inside
from pipeline.walk_coverage import measure
from pipeline.walk_graph import (
    EDGE_LIT,
    EDGE_UNLIT,
    NODE_CROSSING,
    NODE_KERB,
    NODE_KERB_HIGH,
    NODE_MARKED,
    NODE_SIGNALS,
    NODE_UNMARKED,
    NPZ_KEYS,
    build_graph,
)

ROOT = Path(__file__).resolve().parents[1]
BUILT = datetime(2026, 10, 22, 12, 0, tzinfo=ASTANA_TZ)
META = {"osm_snapshot": "2026-05-31", "osm_snapshots": ["2026-05-31", "2026-10-18"]}


def cell(lat: float, lon: float) -> str:
    return h3.latlng_to_cell(lat, lon, 8)


# 0-1: a lit edge and 1-2 an unlit one near Baiterek; 3-4: a 2.8 km edge whose midpoint lies
# in a third cell; 5-6: a stub; 7-8: a 0.3 m edge far away (rounds to 0 m: an empty cell)
NODES = [(51.1280, 71.4300), (51.1282, 71.4310), (51.1284, 71.4320),
         (51.1050, 71.4000), (51.1050, 71.4400),
         (51.2000, 71.5000), (51.2005, 71.5000),
         (51.2500, 71.6000), (51.250002, 71.6000)]
GRAPH = {
    "node_lat": np.array([p[0] for p in NODES], dtype=np.float32),
    "node_lon": np.array([p[1] for p in NODES], dtype=np.float32),
    "node_flags": np.array([
        0,
        NODE_CROSSING | NODE_SIGNALS | NODE_KERB,  # kind known, kerb known
        NODE_CROSSING,  # kind unknown, kerb unknown
        NODE_CROSSING | NODE_MARKED | NODE_KERB_HIGH,  # known, known
        0,
        0,
        NODE_CROSSING | NODE_UNMARKED,  # kind known, kerb unknown
        0, 0], dtype=np.uint8),
    "edge_u": np.array([0, 1, 3, 5, 7], dtype=np.int32),
    "edge_v": np.array([1, 2, 4, 6, 8], dtype=np.int32),
    "edge_len_m": np.array([75.4, 74.6, 2790.2, 55.5, 0.3], dtype=np.float32),
    "edge_flags": np.array([EDGE_LIT, EDGE_UNLIT, 0, 0, 0], dtype=np.uint8),
}


def poi(pid: str, lat: float, lon: float, *, wheelchair: str = "unknown",
        hours: str | None = None, kk: str | None = None,
        fetched: str = "2026-10-18") -> POI:
    return POI.model_validate({
        "id": pid, "name": pid, "category": "cafe", "geo": {"lat": lat, "lon": lon},
        "name_i18n": {"ru": pid} | ({"kk": kk} if kk is not None else {}),
        "wheelchair": wheelchair, "opening_hours": hours, "sources": ["osm"],
        "data_confidence": 0.5, "fetched_at": fetched})


POIS = [
    poi("osm:node/1", 51.1281, 71.4305, wheelchair="yes", hours="Mo-Su 09:00-21:00", kk="Кафе"),
    poi("osm:node/2", 51.1283, 71.4315, hours="  ", kk=""),  # blank hours and kk: no data
    poi("osm:node/3", 51.2002, 71.5001, wheelchair="no", fetched="2026-10-19"),
]


def gaps() -> CityGaps:
    return build_city_gaps(GRAPH, POIS, META, BUILT)


def cells_by_id(g: CityGaps) -> dict:
    return {c.h3_cell: c for c in g.cells}


def test_every_counter_of_a_cell() -> None:
    c = cells_by_id(gaps())[cell(51.1281, 71.4305)]
    assert c.poi == 2 and c.poi_no_hours == 1 and c.poi_wheelchair_unknown == 1
    assert c.poi_no_name_kk == 1  # the empty kk name counts as missing
    assert cell(51.1281, 71.4305) == cell(51.1283, 71.4315)  # both edges' midpoints here
    assert c.walk_m == 150 and c.walk_lit_unknown_m == 0  # one lit, one unlit: both known
    assert c.crossings == 2 and c.crossings_kind_unknown == 1 and c.crossings_kerb_unknown == 1
    stub = cells_by_id(gaps())[cell(51.20025, 71.5)]
    assert stub.walk_m == 56 and stub.walk_lit_unknown_m == 56  # 55.5 rounds to 56
    assert stub.crossings == 1 and stub.crossings_kind_unknown == 0
    assert stub.crossings_kerb_unknown == 1 and stub.poi == 1 and stub.poi_no_hours == 1


def test_an_edge_counts_in_the_cell_of_its_midpoint() -> None:
    mid, ends = cell(51.105, 71.42), {cell(51.105, 71.40), cell(51.105, 71.44)}
    assert mid not in ends
    by = cells_by_id(gaps())
    assert by[mid].walk_m == 2790 and by[mid].walk_lit_unknown_m == 2790
    end_cells = [by[c] for c in ends if c in by]
    assert all(c.walk_m == 0 for c in end_cells)  # node 3 (a crossing) has its cell, no metres
    assert sum(c.crossings for c in end_cells) == 1


def test_empty_cells_are_left_out_and_the_city_is_the_sum() -> None:
    g = gaps()
    assert cell(51.25, 71.6) not in cells_by_id(g)  # 0.3 m rounds to 0, nothing else there
    ids = [c.h3_cell for c in g.cells]
    assert ids == sorted(ids) and len(ids) == len(set(ids))
    for name in GapCounts.model_fields:
        assert getattr(g.city, name) == sum(getattr(c, name) for c in g.cells), name
    assert g.city.poi == 3 and g.city.crossings == 4 and g.city.walk_m == 150 + 2790 + 56


def test_dates_attribution_and_the_same_file_for_the_same_input() -> None:
    g = gaps()
    assert g.poi_date == "2026-10-19" and g.walk_graph_date == "2026-10-18"
    assert newest_snapshot({"osm_snapshot": "2026-05-31T00:00:00Z"}) == "2026-05-31"
    assert g.attribution == "© OpenStreetMap contributors" and g.license == "ODbL-1.0"
    text = dump(g)
    assert CityGaps.model_validate(json.loads(text)) == g
    assert dump(gaps()) == text  # the same input, the same file
    later = dump(build_city_gaps(GRAPH, POIS, META, BUILT + timedelta(hours=1)))
    diff = [a for a, b in zip(text.splitlines(), later.splitlines(), strict=True) if a != b]
    assert len(diff) == 1 and '"built_at"' in diff[0]
    utc = build_city_gaps(GRAPH, POIS, META, datetime(2026, 10, 22, 7, tzinfo=UTC))
    assert utc.built_at.utcoffset() == timedelta(0)
    assert set(json.loads(text)) == set(CityGaps.model_fields)  # aggregates only, no lists


def test_reconcile_with_the_coverage_of_the_same_graph() -> None:
    from tests.test_pipeline_walk_coverage import PAYLOAD

    graph, _ = build_graph([PAYLOAD])
    arrays = {k: getattr(graph, k) for k in NPZ_KEYS}
    g = build_city_gaps(arrays, POIS, META, BUILT)
    cov = measure(graph)
    rows = reconcile(g.city.model_dump(), cov, POIS)
    assert len(rows) == 9 and all(r["ok"] for r in rows), rows
    cov["kerbs"]["unknown"]["nodes"] += 1
    cov["length_km"] += 0.6
    # a poi.jsonl without the second place (blank hours, empty kk, wheelchair unknown)
    bad = {r["what"] for r in reconcile(g.city.model_dump(), cov, [POIS[0], POIS[2]])
           if not r["ok"]}
    assert bad == {"бордюр: нет данных", "длина путей, км", "мест (строк poi.jsonl)",
                   "без часов работы", "wheelchair = unknown", "без названия на казахском"}


def test_committed_city_gaps_file_matches_the_report() -> None:
    processed = ROOT / "data" / "processed"
    g = CityGaps.model_validate_json((processed / "city_gaps.json").read_text(encoding="utf-8"))
    cov = json.loads((processed / "walk_coverage.json").read_text(encoding="utf-8"))
    pois = [POI.model_validate_json(line) for line in
            (processed / "poi.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    rows = reconcile(g.city.model_dump(), cov, pois)
    assert len(rows) == 9 and all(r["ok"] for r in rows), rows
    report = (processed / "REPORT.md").read_text(encoding="utf-8")
    section = report.split("## Карта пробелов", 1)[1].split("\n## ", 1)[0]
    assert "**нет**" not in section and section.count("| да |") == 9
    assert re.search(rf"\| тип перехода \| {g.city.crossings} \| "
                     rf"{g.city.crossings_kind_unknown} \|", section)


# ---------------------------------------------------------------------- districts


def square(s: float, w: float, n: float, e: float) -> list:
    ring = [(s, w), (s, e), (n, e), (n, w), (s, w)]
    return list(zip(ring, ring[1:], strict=False))


A = District("район A", "osm:relation/1", square(51.10, 71.40, 51.12, 71.43))
B = District("район B", "osm:relation/2",
             square(51.12, 71.40, 51.14, 71.43) + square(51.135, 71.425, 51.139, 71.429))


def test_inside_mask_is_the_even_odd_rule_of_normalize_inside() -> None:
    rng = random.Random(7)
    lats = np.array([rng.uniform(51.11, 51.15) for _ in range(400)] + [51.12, 51.135])
    lons = np.array([rng.uniform(71.39, 71.44) for _ in range(400)] + [71.41, 71.427])
    mask = inside_mask(lats, lons, B.edges)
    assert mask.tolist() == [inside(a, b, B.edges) for a, b in zip(lats, lons, strict=True)]
    assert 50 < mask.sum() < 350  # both sides are exercised


def test_district_names_and_tallies() -> None:
    names = district_names(np.array([51.105, 51.13, 51.137, 51.2]),
                           np.array([71.41, 71.41, 71.427, 71.5]), [A, B])
    assert names == ["район A", "район B", OUTSIDE, OUTSIDE]  # 51.137: in B's hole
    by = tally(GRAPH, POIS, lambda la, lo: district_names(la, lo, [A, B]))
    assert by["район A"].lit_m["unknown"] == pytest.approx(2790.2, abs=0.1)  # midpoint in A
    assert sum(sum(t.crossings.values()) for t in by.values()) == 4


def test_districts_from_keeps_the_city_districts_only() -> None:
    def rel(rid: int, name: str | None, ring: list[tuple[float, float]],
            level: str = "6") -> dict:
        tags = {"boundary": "administrative", "admin_level": level} | (
            {"name": name} if name else {})
        return {"type": "relation", "id": rid, "tags": tags, "members": [
            {"type": "way", "role": "outer",
             "geometry": [{"lat": a, "lon": b} for a, b in ring]}]}

    city = [(51.10, 71.40), (51.10, 71.45), (51.15, 71.45), (51.10, 71.40)]
    small = [(51.11, 71.41), (51.11, 71.42), (51.12, 71.42), (51.11, 71.41)]
    region = [(50.50, 70.50), (50.50, 72.50), (52.00, 72.50), (51.10, 71.40)]  # Akmola-sized
    payload = {"elements": [
        rel(1, "Есильский район", city),
        rel(2, "Целиноградский район", region),  # around the city, not a district of it
        rel(3, None, city),  # no name
        rel(4, "Астана", city, level="4"),  # the city itself
        rel(5, "микрорайон", small, level="9"),  # smaller units than the districts
    ]}
    [d] = districts_from(payload)
    assert d.name == "Есильский район" and d.osm_id == "osm:relation/1" and len(d.edges) == 3
    assert d.admin_level == 6 and districts_from({"elements": []}) == []
    assert '["admin_level"~"^(5|6|7|8|9)$"]' in districts_query()


# ---------------------------------------------------------- REPORT and run_all


def test_report_city_gaps_section() -> None:
    from pipeline.normalize import normalize_payload
    from pipeline.report import build_report
    from tests.test_pipeline_report import LANDMARKS, PAYLOAD

    g = build_city_gaps(GRAPH, POIS, META, BUILT).model_dump(mode="json")
    checks = reconcile(g["city"], None, POIS) + [
        {"what": "длина путей, км", "map": 3.0, "report": 3.6, "ok": False}]
    districts = ([A, B], tally(GRAPH, POIS, lambda la, lo: district_names(la, lo, [A, B])))
    pois = normalize_payload(PAYLOAD)
    text = build_report(pois, [PAYLOAD], LANDMARKS, city_gaps=g, city_gaps_checks=checks,
                        city_gaps_districts=districts)
    section = text.split("## Карта пробелов", 1)[1]
    for piece in ("| часы работы | 3 | 2 | 66.7% |", "| тип перехода | 4 | 1 | 25.0% |",
                  "| бордюр на переходе | 4 | 2 | 50.0% |", "Места на 2026-10-19",
                  "© OpenStreetMap contributors, ODbL-1.0", "а не оценка района",
                  "| мест (строк poi.jsonl) | 3 | 3 | да |", "| 3.0 | 3.6 | **нет** |",
                  "| район A | 2.8 | 100.0% |", "| вне районов |", "мало данных",
                  "| без названия на казахском | 2 | 2 | да |"):
        assert piece in section, piece
    # districts: only «нет данных» shares, no «освещено / светофор / часы известны»
    districts_part = section.split("По районам", 1)[1]
    assert "пробелы в открытых данных, а не оценка района" in districts_part
    for word in ("освещено", "светофор", "известны", "занижен"):
        assert word not in districts_part, word
    cells_only = build_report(pois, [PAYLOAD], LANDMARKS, city_gaps=g)
    assert "Районов нет: границ районов OSM на диске нет" in cells_only
    assert "| район A |" not in cells_only


@pytest.fixture
def tmp_data(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from app.config import get_settings

    data = tmp_path / "data"
    (data / "raw").mkdir(parents=True)
    (data / "landmarks.json").write_text("[]", encoding="utf-8")
    payload = {
        "qadam": {"fetched_at_unix": 1792314269, "chunk": "zone_test", "endpoint": "fake"},
        "elements": [{"type": "node", "id": 1, "lat": 51.1283, "lon": 71.4305,
                      "tags": {"amenity": "cafe", "name": "Кофе"}}],
    }
    (data / "raw" / "osm_zone_test.json").write_text(json.dumps(payload), encoding="utf-8")
    for name in ("QADAM_POI_PATH", "QADAM_INDEX_DIR", "QADAM_LANDMARKS_PATH", "QADAM_CACHE_DIR",
                 "QADAM_WALK_GRAPH_PATH", "QADAM_CITY_GAPS_PATH"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("QADAM_DATA_DIR", str(data))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    monkeypatch.setenv("EMBED_PROVIDER", "gemini")
    get_settings.cache_clear()
    yield data
    get_settings.cache_clear()


def test_a_failed_map_step_does_not_stop_run_all(
    tmp_data: Path, caplog: pytest.LogCaptureFixture
) -> None:
    from pipeline.run_all import run

    # no walk graph in the temp data dir: the map cannot be built, the rest goes on
    with caplog.at_level("ERROR", logger="pipeline.run_all"):
        notes = run("astana", skip_fetch=True, skip_enrich=True)
    assert "city gaps: not built" in caplog.text
    assert any("Карта пробелов не собрана" in n for n in notes)
    assert (tmp_data / "processed" / "poi.jsonl").exists()
    assert (tmp_data / "processed" / "REPORT.md").exists()
    assert not (tmp_data / "processed" / "city_gaps.json").exists()


@pytest.mark.parametrize(("hours", "kk", "no_hours", "no_kk"), [
    (None, None, 1, 1), ("", "", 1, 1), ("   ", "  \t", 1, 1), ("24/7", "Кафе", 0, 0),
])
def test_blank_hours_and_kk_name_are_missing(hours: str | None, kk: str | None,
                                             no_hours: int, no_kk: int) -> None:
    place = poi("osm:node/9", 51.1281, 71.4305, hours=hours, kk=kk)
    c = cells_by_id(build_city_gaps(GRAPH, [place], META, BUILT))[cell(51.1281, 71.4305)]
    assert (c.poi, c.poi_no_hours, c.poi_no_name_kk) == (1, no_hours, no_kk)


def test_broken_or_incomplete_files_become_notes_in_the_report(tmp_data: Path) -> None:
    from pipeline.report import write_report
    from pipeline.run_all import run

    run("astana", skip_fetch=True, skip_enrich=True)  # poi.jsonl and a first REPORT.md
    processed = tmp_data / "processed"
    pois = [POI.model_validate_json(line) for line in
            (processed / "poi.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]

    def section() -> str:
        report = write_report("astana").read_text(encoding="utf-8")
        return report.split("## Карта пробелов", 1)[1].split("\n## ", 1)[0]

    (processed / "city_gaps.json").write_text('{"cells": [', encoding="utf-8")
    assert "city_gaps.json не читается как JSON: раздел пропущен" in section()
    (processed / "city_gaps.json").write_text('{"format_version": 1}', encoding="utf-8")
    assert "city_gaps.json не проходит схему CityGaps" in section()

    (processed / "city_gaps.json").write_text(
        dump(build_city_gaps(GRAPH, pois, META, BUILT)), encoding="utf-8")
    (processed / "walk_coverage.json").write_text('{"lit": {}, "length_km": 1}',
                                                  encoding="utf-8")
    text = write_report("astana").read_text(encoding="utf-8")
    assert "Сверка не выполнена" in text  # no crossings / kerbs in walk_coverage.json
    assert "## Покрытие тегов OSM по пешеходному графу\n- Раздел не собран" in text
    (processed / "walk_coverage.json").write_text("not json", encoding="utf-8")
    text = write_report("astana").read_text(encoding="utf-8")
    assert "walk_coverage.json не читается (JSONDecodeError)" in text
    assert "| мест (строк poi.jsonl) | 1 | 1 | да |" in text  # the places are still checked


def test_the_map_file_is_written_whole(tmp_data: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import pipeline.city_gaps as gaps_module
    from pipeline.run_all import run

    run("astana", skip_fetch=True, skip_enrich=True)
    processed = tmp_data / "processed"
    np.savez_compressed(processed / "walk_graph.npz", **GRAPH)
    (processed / "walk_graph_meta.json").write_text(json.dumps(META), encoding="utf-8")
    gaps_module.run(built_at=BUILT)
    out = processed / "city_gaps.json"
    before = out.read_text(encoding="utf-8")
    assert CityGaps.model_validate_json(before).city.walk_m == 150 + 2790 + 56
    assert not list(processed.glob("*.tmp"))

    def broken(gaps: CityGaps) -> str:
        raise RuntimeError("disk full")

    monkeypatch.setattr(gaps_module, "dump", broken)
    with pytest.raises(RuntimeError):
        gaps_module.run(built_at=BUILT)
    assert out.read_text(encoding="utf-8") == before and not list(processed.glob("*.tmp"))
