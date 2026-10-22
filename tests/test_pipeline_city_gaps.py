"""pipeline.city_gaps: the «Карта пробелов» counters on a tiny synthetic graph (no network)."""

from __future__ import annotations

import json
import math
import random
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import h3
import numpy as np
import pytest

from app.hours import ASTANA_TZ
from app.schemas import POI, CityGaps, GapCounts
from pipeline import districts
from pipeline.city_gaps import (
    assign_cells,
    build_city_gaps,
    districts_add_up,
    dump,
    inside_mask,
    newest_snapshot,
    on_line_mask,
    reconcile,
)
from pipeline.districts import district_status
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


def square(s: float, w: float, n: float, e: float) -> list[list[float]]:
    return [[s, w], [s, e], [n, e], [n, w], [s, w]]


def entry(did: str, outline: list, *, relation: int | None = 1, edited: str = "2026-10-01",
          exact: bool = True, kk: str | None = None) -> dict:
    name = {"ru": did, "kk": kk or did, "en": did}
    out = {"id": did, "name": name, "osm_relation": relation, "outline": outline}
    if relation:
        out.update(osm_name={"name": f"{name['kk']} ауданы"}, name_matches=exact,
                   boundary_date=edited)
    return out


# A and B share the line through the centre of the cell near Baiterek; C: no OSM boundary;
# D: a boundary older than the change of the district set that touched it
CENTRE = h3.cell_to_latlng(cell(51.1281, 71.4305))
LINE = CENTRE[1]
DISTRICTS = {
    "format_version": 1,
    "boundaries_date": "2026-10-22",
    "official_list_url": "https://www.gov.kz/org/astana/about/structure/484/1?lang=ru",
    "official_list_checked": "2026-10-22",
    "composition_changes": [{"date": "2024-08", "districts": ["d_old"], "what": "t",
                             "sources": ["https://example.org/act"]}],
    "districts": [
        # A starts at 71.41: the crossing cell at the west end of the long edge is outside
        entry("a_west", [square(51.05, 71.41, 51.15, LINE)]),
        entry("b_east", [square(51.05, LINE, 51.15, 71.45)]),
        entry("c_none", [], relation=None),
        entry("d_old", [square(51.19, 71.49, 51.21, 71.51)], edited="2024-08-20"),
    ],
    "osm_not_in_list": [],
}


def test_inside_mask_is_the_even_odd_rule_of_normalize_inside() -> None:
    rng = random.Random(7)
    ring = [(51.12, 71.40), (51.12, 71.43), (51.14, 71.43), (51.14, 71.40), (51.12, 71.40)]
    hole = [(51.135, 71.425), (51.135, 71.429), (51.139, 71.429), (51.139, 71.425),
            (51.135, 71.425)]
    edges = districts.edges_of([ring, hole])
    lats = np.array([rng.uniform(51.11, 51.15) for _ in range(400)] + [51.12, 51.137])
    lons = np.array([rng.uniform(71.39, 71.44) for _ in range(400)] + [71.41, 71.427])
    mask = inside_mask(lats, lons, edges)
    assert mask.tolist() == [inside(a, b, edges) for a, b in zip(lats, lons, strict=True)]
    assert 50 < mask.sum() < 350 and not mask[-1]  # both sides; the hole is outside


def test_district_status_rules() -> None:
    changes = [{"date": "2023-01-19", "districts": ["x"]}, {"date": "2024-08", "districts": ["x"]}]
    ring = [square(51.0, 71.0, 51.1, 71.1)]
    assert district_status(entry("x", [], relation=None), changes)[0] == "missing"
    assert district_status(entry("x", ring, exact=False), changes)[0] == "stale"
    # a change dated by month counts until the month ends
    assert district_status(entry("x", ring, edited="2024-08-31"), changes)[0] == "stale"
    assert district_status(entry("x", ring, edited="2023-01-18"), changes)[0] == "stale"
    assert district_status(entry("x", ring, edited="2024-09-01"), changes) == (
        "ok", "граница в OSM совпадает со списком")
    assert district_status(entry("y", ring, edited="2020-01-01"), changes)[0] == "ok"


def test_a_cell_goes_to_the_outline_holding_its_centre() -> None:
    west, east, far = cell(51.1, 71.40), cell(51.1, 71.44), cell(51.30, 71.9)
    two = {"b_east": [square(51.05, LINE, 51.15, 71.45)],
           "a_west": [square(51.05, 71.35, 51.15, LINE)]}
    assert assign_cells([west, east, far], two) == {west: "a_west", east: "b_east", far: None}
    # the centre on the shared line: the smaller id; in two outlines: the smaller id too
    on_line = cell(51.1281, 71.4305)
    assert on_line_mask(np.array([CENTRE[0]]), np.array([LINE]),
                        districts.edges_of(two["b_east"]))[0]
    assert assign_cells([on_line], two) == {on_line: "a_west"}
    nested = {"z_big": [square(51.0, 71.3, 51.2, 71.5)],
              "m_small": [square(51.09, 71.39, 51.11, 71.41)]}
    assert assign_cells([west], nested) == {west: "m_small"}


def test_districts_block_sums_statuses_and_unassigned() -> None:
    g = build_city_gaps(GRAPH, POIS, META, BUILT, DISTRICTS)  # the schema checks every sum
    block = g.districts
    assert block is not None and block.official_list_checked == "2026-10-22"
    by = {d.id: d for d in block.items}
    assert {k: v.status for k, v in by.items()} == {
        "a_west": "ok", "b_east": "ok", "c_none": "missing", "d_old": "stale"}
    assert by["c_none"].counts is None and by["c_none"].outline == []
    assert by["c_none"].cell_count == 0 and by["c_none"].osm_relation is None
    assert by["d_old"].boundary_date == "2024-08-20" and by["d_old"].cell_count == 1  # the stub
    assert {c.h3_cell: c.district for c in g.cells}[cell(51.1281, 71.4305)] == "a_west"
    parts = [d.counts for d in block.items if d.counts] + [block.unassigned]
    for name in GapCounts.model_fields:
        assert sum(getattr(p, name) for p in parts) == getattr(g.city, name), name
    assert block.unassigned_cells == sum(1 for c in g.cells if c.district is None) > 0
    assert districts_add_up(g.model_dump(mode="json"))
    assert build_city_gaps(GRAPH, POIS, META, BUILT).districts is None  # no file: as 1.4.0
    text = dump(g)
    assert CityGaps.model_validate(json.loads(text)) == g
    assert '"outline": [\n' in text and "[[51.05, " in text  # a ring on one line


def test_simplify_keeps_within_10_m_and_closes_rings() -> None:
    # a 1 km circle with a point every ~3 m
    ring = [(51.1 + 0.009 * math.sin(t / 1000 * 2 * math.pi),
             71.4 + 0.0143 * math.cos(t / 1000 * 2 * math.pi)) for t in range(1000)]
    ring.append(ring[0])
    out = districts.simplify(ring)
    assert out[0] == out[-1] and 4 <= len(out) < len(ring) / 5
    assert all(round(x, 5) == x for p in out for x in p)
    k = 111_320 * math.cos(math.radians(51.1))

    def dist_m(p: tuple, a: tuple, b: tuple) -> float:
        px, py, ax, ay = p[1] * k, p[0] * 110_574, a[1] * k, a[0] * 110_574
        dx, dy = b[1] * k - ax, b[0] * 110_574 - ay
        t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy or 1)))
        return math.hypot(px - ax - t * dx, py - ay - t * dy)

    edges = list(zip(out, out[1:], strict=False))
    assert max(min(dist_m(p, a, b) for a, b in edges) for p in ring) <= 10.0


def test_an_outline_too_detailed_at_10_m_is_refused() -> None:
    # a zigzag of 30 m teeth: Douglas-Peucker at 8 m keeps every tooth
    zig = [(51.1 + 0.0003 * (i % 2), 71.4 + 0.0001 * i) for i in range(4200)]
    ring = [*zig, (51.09, zig[-1][1]), (51.09, zig[0][1]), zig[0]]
    with pytest.raises(ValueError, match="points"):
        districts.outline_of([ring])


def osm_xml(nodes: dict, ways: dict, relations: dict) -> bytes:
    parts = ['<osm version="0.6">']
    parts += [f'<node id="{n}" lat="{la}" lon="{lo}" timestamp="{ts}"/>'
              for n, (la, lo, ts) in nodes.items()]
    for w, (refs, ts) in ways.items():
        parts.append(f'<way id="{w}" timestamp="{ts}">'
                     + "".join(f'<nd ref="{r}"/>' for r in refs) + "</way>")
    for r, (tags, members, ts) in relations.items():
        parts.append(f'<relation id="{r}" timestamp="{ts}">'
                     + "".join(f'<member type="way" ref="{m}" role="{role}"/>'
                               for m, role in members)
                     + "".join(f'<tag k="{k}" v="{v}"/>' for k, v in tags.items())
                     + "</relation>")
    return ("".join(parts) + "</osm>").encode("utf-8")


def test_rings_join_ways_in_any_order_and_direction() -> None:
    nodes = {1: (51.0, 71.0, "2026-01-01T00:00:00Z"), 2: (51.0, 71.1, "2026-03-05T00:00:00Z"),
             3: (51.1, 71.1, "2026-01-01T00:00:00Z"), 4: (51.1, 71.0, "2026-01-01T00:00:00Z")}
    ways = {10: ([2, 3], "2026-01-01T00:00:00Z"), 11: ([1, 2], "2026-01-01T00:00:00Z"),
            12: ([1, 4, 3], "2026-02-01T00:00:00Z")}  # 12 runs against the others
    full = districts.parse_full(osm_xml(nodes, ways, {
        7: ({"boundary": "administrative", "admin_level": "6"},
            [(10, "outer"), (11, "outer"), (12, "outer")], "2025-12-01T00:00:00Z")}))
    [ring] = districts.rings_of(full, 7)
    assert ring[0] == ring[-1] and len(ring) == 5 and set(ring) == {
        (51.0, 71.0), (51.0, 71.1), (51.1, 71.1), (51.1, 71.0)}
    assert districts.last_edit(full, 7) == "2026-03-05"  # a node edited later than the rest
    del full["relations"][7]["members"][2]
    assert districts.rings_of(full, 7) is None  # an open boundary is not used


def test_names_match_the_official_list() -> None:
    assert districts.match_official({"name": "Есіл ауданы", "name:ru": "район Есиль"}) == (
        "yesil", True)
    assert districts.match_official({"name": "Байқоңыр ауданы"}) == ("baikonyr", True)
    # found only through the Russian or English name: the Kazakh name does not agree
    assert districts.match_official({"name": "Байконыр ауданы", "name:en": "Baikonyr"}) == (
        "baikonyr", False)
    assert districts.match_official({"name": "Целиноград ауданы"}) == (None, False)


def test_fetch_from_a_fake_osm_api(monkeypatch: pytest.MonkeyPatch) -> None:
    """City 900, seed 901 (Есіл), its neighbour 902 found through a shared way (not on the
    list), 903 a district of the region around the city."""
    monkeypatch.setattr(districts, "CITY_RELATION", 900)
    monkeypatch.setattr(districts, "SEED_RELATIONS", (901,))
    ts = "2026-09-01T00:00:00Z"
    coords = [(51.0, 71.0), (51.0, 71.4), (51.3, 71.4), (51.3, 71.0),  # the city: 0-3
              (51.1, 71.1), (51.1, 71.2), (51.2, 71.2), (51.2, 71.1),  # 901: 4-7
              (51.1, 71.3), (51.2, 71.3),  # 902: 5-8-9-6
              (50.0, 70.0), (50.0, 72.0), (52.0, 72.0)]  # 903 around the city: 10-12 (+0)
    nodes = {i: (lat, lon, ts) for i, (lat, lon) in enumerate(coords)}
    ways = {100: ([0, 1, 2, 3, 0], ts), 101: ([4, 5], ts), 102: ([5, 6], ts),
            103: ([6, 7, 4], ts), 104: ([5, 8, 9, 6], ts), 105: ([0, 10, 11, 12, 0], ts)}
    adm = {"boundary": "administrative"}
    rels = {900: ({**adm, "admin_level": "4", "name": "Астана"}, [(100, "outer")], ts),
            901: ({**adm, "admin_level": "6", "name": "Есіл ауданы"},
                  [(101, "outer"), (102, "outer"), (103, "outer")], ts),
            902: ({**adm, "admin_level": "6", "name": "Жаңа ауданы"},
                  [(102, "outer"), (104, "outer")], ts),
            903: ({**adm, "admin_level": "6", "name": "Целиноград ауданы"},
                  [(105, "outer")], ts)}

    def full(rid: int) -> bytes:
        rel = rels[rid]
        wids = [m for m, _ in rel[1]]
        nids = {n for w in wids for n in ways[w][0]}
        return osm_xml({n: nodes[n] for n in nids}, {w: ways[w] for w in wids}, {rid: rel})

    def get(path: str) -> bytes:
        kind, ident = path.strip("/").split("/")[:2]
        if kind == "relation":
            return full(int(ident))
        users = {r: rel for r, rel in rels.items() if any(m == int(ident) for m, _ in rel[1])}
        if int(ident) == 100:
            users[903] = rels[903]  # the region touches the city border
        return osm_xml({}, {}, users)

    data = districts.fetch(get=get)
    by = {d["id"]: d for d in data["districts"]}
    assert by["yesil"]["osm_relation"] == 901 and by["yesil"]["name_matches"] is True
    assert by["yesil"]["boundary_date"] == "2026-09-01" and by["yesil"]["outline"]
    assert by["nura"]["osm_relation"] is None and by["nura"]["outline"] == []
    assert [a["osm_relation"] for a in data["osm_not_in_list"]] == [902]
    assert data["official_list_url"].startswith("https://") and len(data["districts"]) == 6
    assert json.loads(districts.dump(data)) == json.loads(json.dumps(data))


def test_committed_districts_and_the_map() -> None:
    processed = ROOT / "data" / "processed"
    data = json.loads((processed / "districts.json").read_text(encoding="utf-8"))
    g = CityGaps.model_validate_json((processed / "city_gaps.json").read_text(encoding="utf-8"))
    assert g.districts is not None and len(g.districts.items) == len(data["districts"]) == 6
    assert g.districts.official_list_url == data["official_list_url"]
    assert districts_add_up(g.model_dump(mode="json"))
    report = (processed / "REPORT.md").read_text(encoding="utf-8")
    assert "Сверка: районы + вне границ районов = город по всем девяти счётчикам — да." in report
    for d in g.districts.items:
        assert f"| {d.name['ru']} |" in report


def test_report_city_gaps_section() -> None:
    from pipeline.normalize import normalize_payload
    from pipeline.report import build_report
    from tests.test_pipeline_report import LANDMARKS, PAYLOAD

    g = build_city_gaps(GRAPH, POIS, META, BUILT, DISTRICTS).model_dump(mode="json")
    checks = reconcile(g["city"], None, POIS) + [
        {"what": "длина путей, км", "map": 3.0, "report": 3.6, "ok": False}]
    pois = normalize_payload(PAYLOAD)
    text = build_report(pois, [PAYLOAD], LANDMARKS, city_gaps=g, city_gaps_checks=checks,
                        city_gaps_districts=DISTRICTS)
    section = text.split("## Карта пробелов", 1)[1]
    for piece in ("| часы работы | 3 | 2 | 66.7% |", "| тип перехода | 4 | 1 | 25.0% |",
                  "| бордюр на переходе | 4 | 2 | 50.0% |", "Места на 2026-10-19",
                  "© OpenStreetMap contributors, ODbL-1.0", "а не оценка района",
                  "| мест (строк poi.jsonl) | 3 | 3 | да |", "| 3.0 | 3.6 | **нет** |",
                  "| без названия на казахском | 2 | 2 | да |", "сверен с официальным списком",
                  "| c_none | границы нет в OSM | — |",
                  "| d_old | граница в OSM устарела (2024-08-20): граница в OSM (2024-08-20) "
                  "старше изменения состава или границ районов (2024-08) | 1 |",
                  "| вне границ районов | — |", "по всем девяти счётчикам — да.", "мало данных"):
        assert piece in section, piece
    # districts: only «нет данных» shares, no «освещено / светофор / часы известны»
    districts_part = section.split("По районам", 1)[1]
    for word in ("освещено", "светофор", "известны", "занижен"):
        assert word not in districts_part, word
    no_file = build_report(pois, [PAYLOAD], LANDMARKS, city_gaps=build_city_gaps(
        GRAPH, POIS, META, BUILT).model_dump(mode="json"))
    assert "Районов нет: `data/processed/districts.json` нет" in no_file


def test_simplify_measures_to_the_segment_not_the_line() -> None:
    # a spike that runs back along its own line: 500 m out, back to 100 m. Measured to the
    # line, its tip is 0 m off and would be dropped; measured to the segment it is 400 m
    m = 1 / 110_574  # one metre of latitude
    ring = [(51.1, 71.4), (51.1 + 500 * m, 71.4), (51.1 + 100 * m, 71.4),
            (51.1 + 100 * m, 71.401), (51.1, 71.401), (51.1, 71.4)]
    out = districts.simplify(ring)
    assert (round(51.1 + 500 * m, 5), 71.4) in out
    assert districts.max_deviation_m(ring, out) <= 10.0


def test_outline_check_small_rings_and_the_deviation_limit(
        caplog: pytest.LogCaptureFixture) -> None:
    tiny = [(51.10000, 71.40000), (51.10002, 71.40000), (51.10002, 71.40003),
            (51.10000, 71.40003), (51.10000, 71.40000)]  # a 2 m exclave: kept as it is
    assert districts.simplify(tiny) == tiny
    flat = [(51.1, 71.4), (51.1, 71.400001), (51.1, 71.4)]  # fewer than 4 points even unrounded
    big = [(51.0, 71.0), (51.0, 71.1), (51.1, 71.1), (51.1, 71.0), (51.0, 71.0)]
    with caplog.at_level("WARNING", logger="pipeline.districts"):
        assert districts.outline_of([big, flat]) == [big]
    assert "too small to keep" in caplog.text
    circle = [(51.1 + 0.009 * math.sin(t / 200 * 2 * math.pi),
               71.4 + 0.0143 * math.cos(t / 200 * 2 * math.pi)) for t in range(200)]
    circle.append(circle[0])
    with pytest.raises(ValueError, match="off the OSM line"):
        districts.outline_of([circle], max_m=0.5)


def test_api_get_retries_busy_answers_but_not_refusals() -> None:
    import httpx

    answers = iter([httpx.Response(429), httpx.Response(503), httpx.Response(200, text="ok")])
    slept: list[float] = []
    client = httpx.Client(transport=httpx.MockTransport(lambda request: next(answers)))
    assert districts.api_get(client, "/x", pause_s=1.0, sleep=slept.append) == b"ok"
    assert slept == [5.0, 15.0, 1.0]  # two retries, then the steady pace

    refused = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(403)))
    slept.clear()
    with pytest.raises(httpx.HTTPStatusError):
        districts.api_get(refused, "/x", sleep=slept.append)
    assert slept == []  # a refusal is not retried

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    with pytest.raises(RuntimeError, match="ConnectError after 4 tries"):
        districts.api_get(httpx.Client(transport=httpx.MockTransport(down)), "/x",
                          sleep=slept.append)


def _relation_full(rid: int, name: str, ring: list[tuple[float, float]], *, level: str = "6",
                   close: bool = True) -> dict:
    ts = "2026-09-01T00:00:00Z"
    nodes = {rid * 100 + i: (lat, lon, ts) for i, (lat, lon) in enumerate(ring[:-1])}
    refs = list(nodes) + ([rid * 100] if close else [])
    return districts.parse_full(osm_xml(nodes, {rid * 10: (refs, ts)}, {
        rid: ({"boundary": "administrative", "admin_level": level, "name": name},
              [(rid * 10, "outer")], ts)}))


def test_broken_and_duplicate_boundaries_are_said_not_dropped(
        caplog: pytest.LogCaptureFixture) -> None:
    city = [[(51.0, 71.0), (51.0, 71.6), (51.3, 71.6), (51.3, 71.0), (51.0, 71.0)]]

    def box(s: float, w: float) -> list[tuple[float, float]]:
        return [(s, w), (s, w + 0.05), (s + 0.05, w + 0.05), (s + 0.05, w), (s, w)]

    fulls = {
        1: _relation_full(1, "Нұра ауданы", box(51.05, 71.05), close=False),  # does not close
        2: _relation_full(2, "Есіл ауданы", box(51.05, 71.15)),
        3: _relation_full(3, "Есіл ауданы", box(51.05, 71.25)),  # a tie with 2
        4: _relation_full(4, "Алматы ауданы", box(51.15, 71.05), level="8"),
        5: _relation_full(5, "Алматы ауданы", box(51.15, 71.15)),  # level 6 wins over 8
        6: _relation_full(6, "Алмата ауданы", box(51.15, 71.25)),  # not on the list
    }
    with caplog.at_level("WARNING", logger="pipeline.districts"):
        data = districts.build_districts_file(city, fulls)
    assert "relation 1" in caplog.text and "не замыкается" in caplog.text
    by = {d["id"]: d for d in data["districts"]}
    assert by["nura"]["osm_relation"] is None and by["nura"]["unusable"]["osm_relation"] == 1
    assert district_status(by["nura"], [])[0] == "missing"
    assert "relation 1" in district_status(by["nura"], [])[1]
    assert by["yesil"]["osm_relation"] == 2 and by["yesil"]["ambiguous_with"] == [3]
    assert district_status(by["yesil"], [])[0] == "stale"
    assert by["almaty"]["osm_relation"] == 5 and "ambiguous_with" not in by["almaty"]
    assert district_status(by["almaty"], [])[0] == "ok"
    reasons = {a["osm_relation"]: a["why"] for a in data["osm_not_in_list"]}
    assert reasons[1] == "граница не замыкается" and reasons[6] == "нет в официальном списке"
    assert "взята 2" in reasons[3] and "взята 5" in reasons[4]


def test_report_survives_an_incomplete_districts_file() -> None:
    from pipeline.report import city_gaps_lines

    g = build_city_gaps(GRAPH, POIS, META, BUILT, DISTRICTS).model_dump(mode="json")
    broken = {"districts": [{"id": "d_old"}, "junk"], "osm_not_in_list": [{"why": "x"}, 7]}
    text = "\n".join(city_gaps_lines(g, [], broken))
    assert "| d_old | граница в OSM устарела (2024-08-20) | 1 |" in text
    assert "| c_none | границы нет в OSM |" in text
    assert "Граница из OSM не вошла в карту: ? (relation ?) — x." in text


# ---------------------------------------------------------- REPORT and run_all


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
