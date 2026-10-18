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
from app.walkgraph import Mode, WalkGraph, load_graph

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


def test_night_prefers_lit_ways(tmp_path: Path) -> None:
    nodes = {"A": (0, 0, 0), "B": (100, 0, 0), "L1": (0, 15, 0), "L2": (100, 15, 0)}
    edges = [("A", "B", FOOTWAY),  # unlit 100 m: day 80 s, night 120 s
             ("A", "L1", FOOTWAY | LIT), ("L1", "L2", FOOTWAY | LIT), ("L2", "B", FOOTWAY | LIT)]
    g = graph(tmp_path, nodes, edges)  # lit: 130 m = 104 s
    day = g.route(at(0, 0), at(100, 0))
    night = g.route(at(0, 0), at(100, 0), Mode(night=True))
    assert day is not None and night is not None
    assert day.length_m == pytest.approx(100, abs=1)
    assert night.length_m == pytest.approx(130, abs=1)


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


# ----------------------------------------------------------- the real graph


REAL = Path(__file__).resolve().parent.parent / "data" / "processed" / "walk_graph.npz"


@pytest.mark.skipif(not REAL.exists(), reason="walk_graph.npz not built yet (DATA, T93)")
def test_real_graph_median_route_under_100_ms() -> None:
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
        g.route(g.point(int(a)), g.point(b))
        times.append(time.perf_counter() - t0)
    assert len(times) >= 20
    assert statistics.median(times) < 0.1
