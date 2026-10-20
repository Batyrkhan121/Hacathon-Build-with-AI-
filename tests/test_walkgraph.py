"""T91: walking route on the OSM walk graph (synthetic graphs in the ARCHITECTURE §5 format)."""

from __future__ import annotations

import json
import math
import statistics
import time
from pathlib import Path

import numpy as np
import pytest

from app.schemas import GeoPoint
from app.walkgraph import Mode, WalkGraph, crossing_kind, load_graph

LAT0, LON0 = 51.09, 71.41
M_PER_DEG = 111_320.0

# node_flags / edge_flags bits (docs/ARCHITECTURE.md §5)
CROSSING, SIGNALS, LOWERED, UNMARKED = 1, 2, 4, 8
STEPS, LIT, FOOTWAY, MAJOR, CROSS_SEG = 1, 2, 4, 8, 16


def at(x: float, y: float) -> GeoPoint:
    """Point x metres east and y metres north of (LAT0, LON0)."""
    return GeoPoint(lat=LAT0 + y / M_PER_DEG,
                    lon=LON0 + x / (M_PER_DEG * math.cos(math.radians(LAT0))))


def write_graph(
    tmp_path: Path,
    nodes: dict[str, tuple[float, float, int]],
    edges: list[tuple[str, str, int]],
    *,
    poi_nodes: dict[str, str] | None = None,
    meta: dict[str, object] | None = None,
) -> Path:
    """nodes: name -> (x m, y m, node_flags); edges: (a, b, edge_flags), length by straight line."""
    names = list(nodes)
    index = {n: i for i, n in enumerate(names)}
    pts = [at(x, y) for x, y, _ in nodes.values()]
    lengths = [
        math.dist(nodes[a][:2], nodes[b][:2]) for a, b, _ in edges
    ]
    path = tmp_path / "walk_graph.npz"
    np.savez_compressed(
        path,
        node_lat=np.array([p.lat for p in pts], dtype=np.float32),
        node_lon=np.array([p.lon for p in pts], dtype=np.float32),
        node_flags=np.array([f for *_, f in nodes.values()], dtype=np.uint8),
        edge_u=np.array([index[a] for a, _, _ in edges], dtype=np.int32),
        edge_v=np.array([index[b] for _, b, _ in edges], dtype=np.int32),
        edge_len_m=np.array(lengths, dtype=np.float32),
        edge_flags=np.array([f for *_, f in edges], dtype=np.uint8),
    )
    info: dict[str, object] = {
        "osm_snapshot": "2026-10-19", "nodes": len(names), "edges": len(edges),
        "poi_nodes": {k: index[v] for k, v in (poi_nodes or {}).items()},
        "landmark_nodes": {},
    }
    info.update(meta or {})
    (tmp_path / "walk_graph_meta.json").write_text(json.dumps(info), encoding="utf-8")
    return path


def graph(tmp_path: Path, nodes, edges, **kw) -> WalkGraph:  # type: ignore[no-untyped-def]
    g = load_graph(write_graph(tmp_path, nodes, edges, **kw))
    assert g is not None
    return g


# A road between two sidewalks (y=0 and y=30); the only way across is at x=100.
STREET = {
    "S0": (0, 0, 0), "S1": (100, 0, 0), "S2": (200, 0, 0),
    "N0": (0, 30, 0), "N1": (100, 30, 0), "N2": (200, 30, 0),
    "C": (100, 15, CROSSING | SIGNALS),
}
STREET_EDGES = [
    ("S0", "S1", FOOTWAY | LIT), ("S1", "S2", FOOTWAY | LIT),
    ("N0", "N1", FOOTWAY | LIT), ("N1", "N2", FOOTWAY | LIT),
    ("S1", "C", CROSS_SEG | LIT), ("C", "N1", CROSS_SEG | LIT),
]


# ----------------------------------------------------------------- the path


def test_path_goes_round_a_road_without_a_crossing(tmp_path: Path) -> None:
    g = graph(tmp_path, STREET, STREET_EDGES)
    path = g.route(at(0, -5), at(0, 35))
    assert path is not None
    assert path.length_m == pytest.approx(5 + 100 + 30 + 100 + 5, abs=2)
    assert [c.kind for c in path.crossings] == ["signals"]
    assert path.points[0] == pytest.approx((at(0, -5).lat, at(0, -5).lon))
    assert path.points[-1] == pytest.approx((at(0, 35).lat, at(0, 35).lon))
    assert path.walk_min == math.ceil(path.length_m / 75)


def two_crossings(tmp_path: Path) -> WalkGraph:
    nodes = {
        "S0": (100, 0, 0), "S1": (110, 0, 0), "N0": (100, 30, 0), "N1": (110, 30, 0),
        "CU": (100, 15, CROSSING | UNMARKED), "CS": (110, 15, CROSSING | SIGNALS),
    }
    edges = [
        ("S0", "S1", FOOTWAY), ("N0", "N1", FOOTWAY),
        ("S0", "CU", CROSS_SEG), ("CU", "N0", CROSS_SEG),
        ("S1", "CS", CROSS_SEG), ("CS", "N1", CROSS_SEG),
    ]
    return graph(tmp_path, nodes, edges)


def test_prefers_the_crossing_with_traffic_lights(tmp_path: Path) -> None:
    # unmarked: 30 m (24 s) + 40 s; signals: 50 m (40 s) + 20 s -> signals
    path = two_crossings(tmp_path).route(at(100, 0), at(100, 30))
    assert path is not None
    assert [c.kind for c in path.crossings] == ["signals"]


def test_with_kids_unmarked_crossing_costs_more(tmp_path: Path) -> None:
    nodes = {
        "S0": (100, 0, 0), "S1": (120, 0, 0), "N0": (100, 30, 0), "N1": (120, 30, 0),
        "CU": (100, 15, CROSSING | UNMARKED), "CS": (120, 15, CROSSING | SIGNALS),
    }
    edges = [
        ("S0", "S1", FOOTWAY), ("N0", "N1", FOOTWAY),
        ("S0", "CU", CROSS_SEG), ("CU", "N0", CROSS_SEG),
        ("S1", "CS", CROSS_SEG), ("CS", "N1", CROSS_SEG),
    ]
    g = graph(tmp_path, nodes, edges)
    # default: unmarked 24+40=64 s < signals 70 m = 56+20=76 s; kids: unmarked 24+60=84 > 76
    default = g.route(at(100, 0), at(100, 30))
    kids = g.route(at(100, 0), at(100, 30), Mode(kids=True))
    assert default is not None and kids is not None
    assert [c.kind for c in default.crossings] == ["unmarked"]
    assert [c.kind for c in kids.crossings] == ["signals"]


def stairs_or_ramp(tmp_path: Path) -> WalkGraph:
    nodes = {"A": (0, 0, 0), "M": (0, 20, 0), "B": (0, 40, 0), "R1": (30, 0, 0),
             "R2": (30, 40, 0)}
    edges = [("A", "M", STEPS), ("M", "B", STEPS),
             ("A", "R1", FOOTWAY), ("R1", "R2", FOOTWAY), ("R2", "B", FOOTWAY)]
    return graph(tmp_path, nodes, edges)


def test_wheelchair_never_takes_stairs(tmp_path: Path) -> None:
    g = stairs_or_ramp(tmp_path)
    walk = g.route(at(0, 0), at(0, 40))  # stairs: 40 m x2 = 64 s < ramp 100 m = 80 s
    chair = g.route(at(0, 0), at(0, 40), Mode(wheelchair=True))
    assert walk is not None and chair is not None
    assert walk.steps == 2
    assert chair.steps == 0
    assert chair.length_m == pytest.approx(100, abs=1)


def test_night_prefers_lit_ways_a_little(tmp_path: Path) -> None:
    nodes = {"A": (0, 0, 0), "B": (100, 0, 0), "M": (50, 15, 0)}
    edges = [("A", "B", FOOTWAY),  # no lit bit, 100 m: day 80 s, night 88 s
             ("A", "M", FOOTWAY | LIT), ("M", "B", FOOTWAY | LIT)]  # lit 104 m = 83.5 s
    g = graph(tmp_path, nodes, edges)
    day = g.route(at(0, 0), at(100, 0))
    night = g.route(at(0, 0), at(100, 0), Mode(night=True))
    assert day is not None and night is not None
    assert day.length_m == pytest.approx(100, abs=1)
    assert night.length_m == pytest.approx(104.4, abs=1)


def test_night_does_not_chase_rare_lit_ways(tmp_path: Path) -> None:
    """No lit bit means «unknown» (lit is tagged on ~3% of Astana edges), not «dark»."""
    nodes = {"A": (0, 0, 0), "B": (100, 0, 0), "L1": (0, 15, 0), "L2": (100, 15, 0)}
    edges = [("A", "B", FOOTWAY),  # 100 m, night 88 s
             ("A", "L1", FOOTWAY | LIT), ("L1", "L2", FOOTWAY | LIT), ("L2", "B", FOOTWAY | LIT)]
    night = graph(tmp_path, nodes, edges).route(at(0, 0), at(100, 0), Mode(night=True))
    assert night is not None  # the lit way is 130 m = 104 s: not worth it
    assert night.length_m == pytest.approx(100, abs=1)


def test_prefers_footways_to_big_roads(tmp_path: Path) -> None:
    nodes = {"A": (0, 0, 0), "B": (100, 0, 0), "F1": (0, 10, 0), "F2": (100, 10, 0)}
    edges = [("A", "B", MAJOR),  # 100 m x1.3 = 104 s
             ("A", "F1", FOOTWAY), ("F1", "F2", FOOTWAY), ("F2", "B", FOOTWAY)]  # 120 m = 96 s
    path = graph(tmp_path, nodes, edges).route(at(0, 0), at(100, 0))
    assert path is not None
    assert path.length_m == pytest.approx(120, abs=1)


# ------------------------------------------------------------ snapping


def test_snap_within_150_m_only(tmp_path: Path) -> None:
    g = graph(tmp_path, STREET, STREET_EDGES)
    assert g.nearest_node(at(0, -100)) is not None
    assert g.nearest_node(at(0, -160)) is None
    assert g.route(at(0, -160), at(0, 35)) is None


def test_poi_nodes_from_meta_are_used(tmp_path: Path) -> None:
    g = graph(tmp_path, STREET, STREET_EDGES, poi_nodes={"osm:node/1": "N1"})
    # the POI sits 20 m north of N1, nearer to nothing else; meta says its node is N1
    path = g.route(at(0, -5), at(100, 50), b_poi="osm:node/1")
    assert path is not None
    assert path.points[-2] == pytest.approx((at(100, 30).lat, at(100, 30).lon), abs=1e-5)


# ----------------------------------------------------------------- failures


def test_no_path_between_components(tmp_path: Path) -> None:
    nodes = {"A": (0, 0, 0), "B": (50, 0, 0), "C": (0, 100, 0), "D": (50, 100, 0)}
    g = graph(tmp_path, nodes, [("A", "B", FOOTWAY), ("C", "D", FOOTWAY)])
    assert g.route(at(0, 0), at(0, 100)) is None


def test_time_limit_gives_up(tmp_path: Path) -> None:
    g = graph(tmp_path, STREET, STREET_EDGES)
    assert g.route(at(0, -5), at(0, 35), limit_s=0.0) is None


def test_missing_or_broken_files_load_as_none(tmp_path: Path) -> None:
    assert load_graph(tmp_path / "nope.npz") is None
    bad = tmp_path / "bad"
    bad.mkdir()
    np.savez_compressed(bad / "walk_graph.npz", node_lat=np.zeros(2, np.float32))
    assert load_graph(bad / "walk_graph.npz") is None
    newer = tmp_path / "v2"
    newer.mkdir()
    assert load_graph(write_graph(newer, STREET, STREET_EDGES, meta={"format_version": 2})) is None


def test_loaded_once(tmp_path: Path) -> None:
    path = write_graph(tmp_path, STREET, STREET_EDGES)
    assert load_graph(path) is load_graph(path)


def test_dead_end_crossings_and_sub_cm_edges_are_tolerated(tmp_path: Path) -> None:
    """ARCHITECTURE §5: OSM has crossing tails of degree 1 and coincident nodes."""
    nodes = {"A": (0, 0, 0), "A2": (0.004, 0, 0), "M": (50, 0, 0), "B": (100, 0, 0),
             "T": (50, 10, CROSSING | SIGNALS)}  # a crossing node hanging off the way
    edges = [("A", "A2", FOOTWAY), ("A2", "M", FOOTWAY), ("M", "B", FOOTWAY),
             ("M", "T", CROSS_SEG)]
    g = graph(tmp_path, nodes, edges)
    path = g.route(at(0, 0), at(100, 0))
    assert path is not None and not path.straight
    assert path.length_m == pytest.approx(100, abs=1)
    assert path.crossings == []  # the tail is not walked


# ---------------------------------------- review BLOCK: binding far, points near


def test_near_points_bound_far_away_get_the_straight_leg(tmp_path: Path) -> None:
    """Two places 3 m apart bound to one node 80 m away: ≈3 m, not ≈160 m (E4 Fame City)."""
    g = graph(tmp_path, {"A": (0, 0, 0), "B": (500, 0, 0)}, [("A", "B", FOOTWAY)])
    path = g.route(at(80, 0), at(80, 3))
    assert path is not None
    assert path.length_m == pytest.approx(3, abs=0.5)
    assert path.straight and path.walk_min == 1 and path.crossings == []
    assert "x" not in g.lengths_from(at(80, 0), {"x": at(80, 3)})  # card keeps the straight


def test_detour_over_three_times_plus_200_m_gets_the_straight_leg(tmp_path: Path) -> None:
    nodes = {"A": (0, 0, 0), "A2": (0, 600, 0), "B2": (60, 600, 0), "B": (60, 0, 0)}
    edges = [("A", "A2", FOOTWAY), ("A2", "B2", FOOTWAY), ("B2", "B", FOOTWAY)]
    g = graph(tmp_path, nodes, edges)  # graph 1260 m > 3 x 60 + 200 = 380 m
    path = g.route(at(0, 0), at(60, 0))
    assert path is not None and path.straight
    assert path.length_m == pytest.approx(60, abs=1)
    assert path.walk_min == 2  # the straight rule: ceil(60 x 1.3 / 75)
    assert "b" not in g.lengths_from(at(0, 0), {"b": at(60, 0)})


# ------------------------------------------------------- legs and many targets


def test_route_through_stops_shares_one_deadline(tmp_path: Path) -> None:
    g = graph(tmp_path, STREET, STREET_EDGES)
    legs = g.route_through([at(0, -5), at(200, -5), at(0, 35)])
    assert legs is not None and len(legs) == 2
    assert legs[1].crossings[0].kind == "signals"
    assert g.route_through([at(0, -5), at(200, -5)], limit_s=0.0) is None


def test_lengths_from_one_search(tmp_path: Path) -> None:
    g = graph(tmp_path, STREET, STREET_EDGES)
    lengths = g.lengths_from(at(0, -5), {"a": at(200, -5), "b": at(0, 35), "far": at(0, -400)})
    assert lengths["a"] == pytest.approx(5 + 200 + 5, abs=2)
    assert lengths["b"] == pytest.approx(240, abs=3)
    assert "far" not in lengths  # cannot be snapped: the caller keeps the straight estimate


# ------------------------------------------- wave 8: route profiles in the edge costs


UNLIT, KERB_RAISED = 32, 16  # edge: lit=no; node: kerb known and not lowered
MARKINGS = 32  # node: markings known (crossing=marked / zebra / uncontrolled)


def stairs_in_the_middle(tmp_path: Path, detour_y: float | None) -> WalkGraph:
    """A 500 m way A -> B with 20 m of stairs at x=240..260; a ramp round them via y=detour_y."""
    nodes = {"A": (0, 0, 0), "S1": (240, 0, 0), "S2": (260, 0, 0), "B": (500, 0, 0)}
    edges = [("A", "S1", FOOTWAY), ("S1", "S2", STEPS), ("S2", "B", FOOTWAY)]
    if detour_y is not None:
        nodes |= {"R1": (240, detour_y, 0), "R2": (260, detour_y, 0)}
        edges += [("S1", "R1", FOOTWAY), ("R1", "R2", FOOTWAY), ("R2", "S2", FOOTWAY)]
    return graph(tmp_path, nodes, edges)


def test_accessible_goes_round_the_stairs_when_it_costs_up_to_30_percent(tmp_path: Path) -> None:
    g = stairs_in_the_middle(tmp_path, 30)  # ramp: 560 m, 12% longer than the stairs way
    fastest = g.route(at(0, 0), at(500, 0))
    accessible = g.route(at(0, 0), at(500, 0), Mode(profile="accessible"))
    assert fastest is not None and accessible is not None
    assert fastest.steps == 1 and fastest.length_m == pytest.approx(500, abs=1)
    assert accessible.steps == 0 and accessible.length_m == pytest.approx(560, abs=1)
    assert not accessible.stairs_fallback


@pytest.mark.parametrize("detour_y", [100, None], ids=["ramp_40_percent_longer", "no_ramp"])
def test_accessible_falls_back_to_the_stairs_way(tmp_path: Path, detour_y: float | None) -> None:
    """No step-free way, or one over 30% longer (700 m vs 500 m): the usual route, flagged."""
    g = stairs_in_the_middle(tmp_path, detour_y)
    path = g.route(at(0, 0), at(500, 0), Mode(profile="accessible"))
    assert path is not None and not path.straight
    assert path.steps == 1 and path.length_m == pytest.approx(500, abs=1)
    assert path.stairs_fallback


def test_wheelchair_still_never_takes_stairs_even_as_a_fallback(tmp_path: Path) -> None:
    g = stairs_in_the_middle(tmp_path, None)
    assert g.route(at(0, 0), at(500, 0), Mode(wheelchair=True, profile="accessible")) is None


def test_accessible_prefers_traffic_lights_to_an_unmarked_crossing(tmp_path: Path) -> None:
    nodes = {
        "S0": (0, 0, 0), "S1": (60, 0, 0), "N0": (0, 30, 0), "N1": (60, 30, 0),
        "CU": (0, 15, CROSSING | UNMARKED), "CS": (60, 15, CROSSING | SIGNALS),
    }
    edges = [
        ("S0", "S1", FOOTWAY), ("N0", "N1", FOOTWAY),
        ("S0", "CU", CROSS_SEG), ("CU", "N0", CROSS_SEG),
        ("S1", "CS", CROSS_SEG), ("CS", "N1", CROSS_SEG),
    ]
    g = graph(tmp_path, nodes, edges)
    # fastest: unmarked 30 m (24 s) + 40 s = 64 s < lights 150 m (120 s) + 20 s
    fastest = g.route(at(0, 0), at(0, 30))
    accessible = g.route(at(0, 0), at(0, 30), Mode(profile="accessible"))
    assert fastest is not None and accessible is not None
    assert [c.kind for c in fastest.crossings] == ["unmarked"]
    assert [c.kind for c in accessible.crossings] == ["signals"]


@pytest.mark.parametrize(("kerb_flags", "kind"), [(LOWERED, "lowered"), (KERB_RAISED, "raised")])
def test_accessible_prefers_crossings_with_lowered_kerbs(
    tmp_path: Path, kerb_flags: int, kind: str
) -> None:
    nodes = {
        "S0": (0, 0, 0), "S1": (5, 0, kerb_flags), "N0": (0, 30, 0), "N1": (5, 30, kerb_flags),
        "CU": (0, 15, CROSSING), "CK": (5, 15, CROSSING),
    }
    edges = [
        ("S0", "S1", FOOTWAY), ("N0", "N1", FOOTWAY),
        ("S0", "CU", CROSS_SEG), ("CU", "N0", CROSS_SEG),
        ("S1", "CK", CROSS_SEG), ("CK", "N1", CROSS_SEG),
    ]
    g = graph(tmp_path, nodes, edges)
    # 30 m (24 s) + 10 s with no kerb data vs 40 m (32 s) + 10 s with kerb data at both ends
    fastest = g.route(at(0, 0), at(0, 30))
    accessible = g.route(at(0, 0), at(0, 30), Mode(profile="accessible"))
    assert fastest is not None and accessible is not None
    assert [c.kerb for c in fastest.crossings] == ["unknown"]
    expected = "lowered" if kind == "lowered" else "unknown"
    assert [c.kerb for c in accessible.crossings] == [expected]


def lit_or_not(tmp_path: Path, direct_flags: int, lit_way_m: float) -> WalkGraph:
    """A -> B: a direct 100 m way with `direct_flags`, or a lit way of `lit_way_m` via M."""
    half = lit_way_m / 2
    nodes = {"A": (0, 0, 0), "B": (100, 0, 0), "M": (50, math.sqrt(half * half - 50 * 50), 0)}
    edges = [("A", "B", FOOTWAY | direct_flags),
             ("A", "M", FOOTWAY | LIT), ("M", "B", FOOTWAY | LIT)]
    return graph(tmp_path, nodes, edges)


def test_lit_profile_avoids_ways_known_to_be_dark(tmp_path: Path) -> None:
    g = lit_or_not(tmp_path, UNLIT, 130)
    fastest = g.route(at(0, 0), at(100, 0))
    lit = g.route(at(0, 0), at(100, 0), Mode(profile="lit"))
    assert fastest is not None and lit is not None
    assert fastest.length_m == pytest.approx(100, abs=1)
    assert lit.length_m == pytest.approx(130, abs=1)


@pytest.mark.parametrize(("lit_way_m", "expected_m"), [(115, 115), (130, 100)])
def test_lit_profile_pays_a_little_for_ways_without_data(
    tmp_path: Path, lit_way_m: float, expected_m: float
) -> None:
    """No data is a little worse than lit (115 m lit beats 100 m unknown), not «dark»."""
    g = lit_or_not(tmp_path, 0, lit_way_m)
    lit = g.route(at(0, 0), at(100, 0), Mode(profile="lit"))
    night = g.route(at(0, 0), at(100, 0), Mode(night=True))
    assert lit is not None and night is not None
    assert lit.length_m == pytest.approx(expected_m, abs=1)
    assert night.length_m == pytest.approx(100, abs=1)  # fastest at night: as before


def test_lit_shares_of_the_path(tmp_path: Path) -> None:
    nodes = {"A": (0, 0, 0), "B": (100, 0, 0), "C": (150, 0, 0), "D": (200, 0, 0),
             "E": (220, 0, 0)}
    edges = [("A", "B", FOOTWAY | LIT), ("B", "C", FOOTWAY | UNLIT), ("C", "D", FOOTWAY),
             ("D", "E", FOOTWAY | LIT | UNLIT)]  # contradictory tags: no data, never «lit»
    path = graph(tmp_path, nodes, edges).route(at(0, -5), at(220, 0))
    assert path is not None and not path.straight
    assert path.lit_m == pytest.approx(100, abs=0.5)
    assert path.unlit_m == pytest.approx(50, abs=0.5)
    assert path.unknown_lit_m == pytest.approx(5 + 50 + 20, abs=0.5)  # the link has no data
    assert path.lit_m + path.unlit_m + path.unknown_lit_m == pytest.approx(path.length_m)


def test_a_straight_leg_has_no_lighting_data(tmp_path: Path) -> None:
    g = graph(tmp_path, {"A": (0, 0, 0), "B": (500, 0, 0)}, [("A", "B", FOOTWAY | LIT)])
    path = g.route(at(80, 0), at(80, 3))
    assert path is not None and path.straight
    assert path.lit_m == 0 and path.unlit_m == 0
    assert path.unknown_lit_m == pytest.approx(path.length_m)


@pytest.mark.parametrize(
    ("crossing", "south", "north", "kerb"),
    [
        (CROSSING | SIGNALS | LOWERED, 0, 0, "lowered"),  # on the crossing node itself
        (CROSSING | SIGNALS, LOWERED, LOWERED, "lowered"),  # kerb nodes at both ends
        (CROSSING | SIGNALS, LOWERED, 0, "unknown"),  # one side only: not «yes»
        (CROSSING | SIGNALS, 0, 0, "unknown"),
        (CROSSING | KERB_RAISED, 0, 0, "raised"),
        (CROSSING | LOWERED | KERB_RAISED, 0, 0, "unknown"),  # contradictory tags
    ],
)
def test_kerb_of_each_crossing(
    tmp_path: Path, crossing: int, south: int, north: int, kerb: str
) -> None:
    nodes = {"S": (0, 0, south), "C": (0, 15, crossing), "N": (0, 30, north)}
    edges = [("S", "C", CROSS_SEG), ("C", "N", CROSS_SEG)]
    path = graph(tmp_path, nodes, edges).route(at(0, -1), at(0, 31))
    assert path is not None
    assert [c.kerb for c in path.crossings] == [kerb]


def test_kerb_of_a_crossing_segment_without_a_crossing_node(tmp_path: Path) -> None:
    nodes = {"S": (0, 0, LOWERED), "N": (0, 30, LOWERED)}
    path = graph(tmp_path, nodes, [("S", "N", CROSS_SEG)]).route(at(0, -1), at(0, 31))
    assert path is not None
    assert [(c.kind, c.kerb) for c in path.crossings] == [("unknown", "lowered")]


@pytest.mark.parametrize(
    ("flags", "kind"),
    [
        (CROSSING | SIGNALS, "signals"),
        (CROSSING | SIGNALS | MARKINGS, "signals"),
        (CROSSING | UNMARKED, "unmarked"),
        (CROSSING | UNMARKED | MARKINGS, "unmarked"),  # bit 8 before bit 32
        (CROSSING | MARKINGS, "marked"),
        (CROSSING, "unknown"),  # OSM gives no crossing type: never shown as a zebra
        (CROSSING | LOWERED, "unknown"),
    ],
)
def test_crossing_kind_from_the_bits(tmp_path: Path, flags: int, kind: str) -> None:
    nodes = {"S": (0, 0, 0), "C": (0, 15, flags), "N": (0, 30, 0)}
    path = graph(tmp_path, nodes, [("S", "C", CROSS_SEG), ("C", "N", CROSS_SEG)]).route(
        at(0, -1), at(0, 31))
    assert path is not None
    assert [c.kind for c in path.crossings] == [kind]


def test_crossing_kind_without_the_crossing_bit() -> None:
    assert crossing_kind(SIGNALS) == "signals"  # traffic lights on the road itself
    assert crossing_kind(MARKINGS) == "marked"
    assert crossing_kind(0) is None and crossing_kind(LOWERED) is None


def near_or_far_crossing(tmp_path: Path, near: int, far: int, far_x: float) -> WalkGraph:
    """Across a road from (0, 0) to (0, 30): a crossing right there or one far_x m along."""
    nodes = {
        "S0": (0, 0, 0), "S1": (far_x, 0, 0), "N0": (0, 30, 0), "N1": (far_x, 30, 0),
        "CN": (0, 15, near), "CF": (far_x, 15, far),
    }
    edges = [
        ("S0", "S1", FOOTWAY), ("N0", "N1", FOOTWAY),
        ("S0", "CN", CROSS_SEG), ("CN", "N0", CROSS_SEG),
        ("S1", "CF", CROSS_SEG), ("CF", "N1", CROSS_SEG),
    ]
    return graph(tmp_path, nodes, edges)


@pytest.mark.parametrize(
    ("profile", "near", "far", "far_x", "taken"),
    [
        # fastest: unknown +20 s, between marked +10 s and unmarked +40 s
        ("fastest", CROSSING | UNMARKED, CROSSING, 10, "unknown"),  # 24+40 > 40+20
        ("fastest", CROSSING, CROSSING | MARKINGS, 5, "marked"),  # 24+20 > 32+10
        ("fastest", CROSSING, CROSSING | MARKINGS, 20, "unknown"),  # 24+20 < 56+10
        # accessible: unknown +60 s, between marked +10 s and unmarked +120 s (kerbs +15 both)
        ("accessible", CROSSING, CROSSING | MARKINGS, 20, "marked"),  # 24+60 > 56+10
        ("accessible", CROSSING | UNMARKED, CROSSING, 30, "unknown"),  # 24+120 > 72+60
    ],
)
def test_unknown_crossings_cost_between_marked_and_unmarked(
    tmp_path: Path, profile: str, near: int, far: int, far_x: float, taken: str
) -> None:
    g = near_or_far_crossing(tmp_path, near, far, far_x)
    path = g.route(at(0, 0), at(0, 30), Mode(profile=profile))  # type: ignore[arg-type]
    assert path is not None
    assert [c.kind for c in path.crossings] == [taken]


@pytest.mark.parametrize(("detour_y", "expected_m"), [(30, 560), (100, 500), (None, 500)])
def test_card_lengths_follow_the_accessible_rule(
    tmp_path: Path, detour_y: float | None, expected_m: float
) -> None:
    g = stairs_in_the_middle(tmp_path, detour_y)
    lengths = g.lengths_from(at(0, 0), {"b": at(500, 0)}, Mode(profile="accessible"))
    assert lengths["b"] == pytest.approx(expected_m, abs=1)
    assert g.lengths_from(at(0, 0), {"b": at(500, 0)})["b"] == pytest.approx(500, abs=1)


# ----------------------------------------------------------- the real graph


REAL =Path(__file__).resolve().parent.parent / "data" / "processed" / "walk_graph.npz"


@pytest.mark.skipif(not REAL.exists(), reason="walk_graph.npz not built yet (DATA, T93)")
@pytest.mark.parametrize("profile", ["fastest", "accessible", "lit"])
def test_real_graph_median_route_under_100_ms(profile: str) -> None:
    g = load_graph(REAL)
    assert g is not None
    rng = np.random.default_rng(19)
    lat, lon = g.node_lat, g.node_lon
    kx = M_PER_DEG * math.cos(math.radians(float(lat.mean())))
    times = []
    for a in rng.choice(len(lat), size=40, replace=False):
        # the second point near the first: card / route distances are 0.3-2 km
        d = np.hypot((lat - lat[a]) * M_PER_DEG, (lon - lon[a]) * kx)
        near = np.flatnonzero((d >= 300) & (d <= 2000))
        if not len(near):
            continue
        b = int(rng.choice(near))
        t0 = time.perf_counter()
        g.route(g.point(int(a)), g.point(b), Mode(profile=profile))  # type: ignore[arg-type]
        times.append(time.perf_counter() - t0)
    assert len(times) >= 20
    assert statistics.median(times) < 0.1
