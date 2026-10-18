"""pipeline.walk_graph on a tiny synthetic OSM answer (no network).

The format checks follow the DATA -> FEATURES contract in docs/ARCHITECTURE.md section 5.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from pipeline.walk_graph import (
    EDGE_CROSSING,
    EDGE_FOOTWAY,
    EDGE_LIT,
    EDGE_MAJOR,
    EDGE_STEPS,
    NODE_CROSSING,
    NODE_KERB,
    NODE_SIGNALS,
    NODE_UNMARKED,
    NPZ_KEYS,
    build_graph,
    load_graph,
    save_graph,
    snap_points,
    walk_query,
)

LAT0, LON0 = 51.1280, 71.4300
DLAT = 0.0009  # ~100 m
DLON = 0.00143  # ~100 m at this latitude


def n(i: int, row: float, col: float, **tags: str) -> dict:
    return {"type": "node", "id": i, "lat": LAT0 + row * DLAT, "lon": LON0 + col * DLON,
            **({"tags": tags} if tags else {})}


def w(i: int, nodes: list[int], **tags: str) -> dict:
    return {"type": "way", "id": i, "nodes": nodes, "tags": tags}


PAYLOAD = {
    "osm3s": {"timestamp_osm_base": "2026-10-19T08:00:00Z"},
    "elements": [
        n(1, 0, 0), n(2, 0, 1), n(3, 0, 2, highway="crossing", crossing="zebra"),
        n(4, 0, 3, highway="traffic_signals"), n(5, 1, 3, kerb="lowered"),
        n(6, 1, 2, highway="crossing", crossing="unmarked"),
        n(7, 2, 3), n(8, 2, 2), n(9, 5, 5), n(10, 5, 6), n(11, 0.5, 2.5),
        # footway 1-2-3-4: node 2 is a plain point and is compressed into the edge 1-3
        w(100, [1, 2, 3, 4], highway="footway"),
        w(101, [4, 5], highway="primary", lit="yes"),  # a major road
        w(102, [3, 6], highway="steps", lit="yes"),
        w(103, [5, 6], highway="footway", footway="crossing"),
        w(104, [5, 7], highway="motorway"),  # not walkable
        w(105, [6, 8], highway="footway", foot="no"),  # not walkable
        w(106, [7, 8], highway="service", access="private"),  # not walkable
        w(107, [9, 10], highway="footway"),  # a separate small component: dropped
        w(108, [3, 11, 4], highway="path"),  # a longer parallel way 3-4: merged away
    ],
}


@pytest.fixture(scope="module")
def built():
    return build_graph([PAYLOAD])


def edge_index(graph, a: int, b: int) -> int:
    ids = graph.node_osm_id
    for k in range(len(graph.edge_u)):
        if {int(ids[graph.edge_u[k]]), int(ids[graph.edge_v[k]])} == {a, b}:
            return k
    raise AssertionError((a, b))


def test_query_has_walkable_highways_and_exclusions() -> None:
    q = walk_query("51.03,71.35,51.11,71.5")
    for value in ("footway", "steps", "living_street", "primary", "corridor", "track"):
        assert value in q
    assert "motorway" not in q and "trunk" not in q
    assert '["foot"!~"^no$"]' in q and '["access"!~"^private$"]' in q
    assert q.rstrip().endswith("out body qt;")


def test_split_compress_largest_component_and_single_edges(built) -> None:
    graph, stats = built
    assert set(graph.node_osm_id.tolist()) == {1, 3, 4, 5, 6}  # 2, 11 compressed; 7-10 gone
    pairs = [tuple(sorted((int(graph.node_osm_id[u]), int(graph.node_osm_id[v]))))
             for u, v in zip(graph.edge_u, graph.edge_v, strict=True)]
    assert sorted(pairs) == [(1, 3), (3, 4), (3, 6), (4, 5), (5, 6)]  # each edge once
    assert all(u != v for u, v in zip(graph.edge_u, graph.edge_v, strict=True))
    assert stats["parallel_edges_merged"] == 1
    assert graph.edge_len_m[edge_index(graph, 3, 4)] == pytest.approx(100, rel=0.02)  # shortest
    assert graph.edge_len_m[edge_index(graph, 1, 3)] == pytest.approx(200, rel=0.02)  # 1-2-3
    assert stats["components_dropped"] == 1 and stats["ways_excluded"] == 3
    # kept-node graph before the merge: {1, 3, 4, 5, 6} and {9, 10}
    assert stats["largest_component_share"] == pytest.approx(5 / 7, abs=1e-4)


def test_node_and_edge_flags(built) -> None:
    graph, stats = built
    flags = dict(zip(graph.node_osm_id.tolist(), graph.node_flags.tolist(), strict=True))
    assert flags[3] == NODE_CROSSING and flags[4] == NODE_SIGNALS and flags[5] == NODE_KERB
    assert flags[6] == NODE_CROSSING | NODE_UNMARKED and flags[1] == 0

    def ef(a: int, b: int) -> int:
        return int(graph.edge_flags[edge_index(graph, a, b)])

    assert ef(3, 6) == EDGE_STEPS | EDGE_LIT | EDGE_FOOTWAY
    assert ef(4, 5) == EDGE_MAJOR | EDGE_LIT
    assert ef(1, 3) == EDGE_FOOTWAY and ef(5, 6) == EDGE_FOOTWAY | EDGE_CROSSING
    assert stats["crossings"] == 2 and stats["signals"] == 1 and stats["unmarked_crossings"] == 1
    assert stats["kerbs_lowered"] == 1 and stats["steps_edges"] == 1


def test_snap_within_150_m(built) -> None:
    graph, _ = built
    snaps = snap_points(graph, [
        ("osm:node/500", LAT0 + 1.4 * DLAT, LON0 + 3 * DLON),  # ~40 m from node 5
        ("osm:node/501", LAT0 + 4 * DLAT, LON0),  # ~360 m from everything
    ])
    assert list(snaps) == ["osm:node/500"]
    node, dist = snaps["osm:node/500"]
    assert int(graph.node_osm_id[node]) == 5 and dist == pytest.approx(40, abs=5)


def test_save_follows_the_contract(built, tmp_path: Path) -> None:
    graph, stats = built
    poi = snap_points(graph, [("osm:node/500", LAT0 + DLAT, LON0 + 3 * DLON)])
    lm = snap_points(graph, [("expo", LAT0, LON0)])
    npz, meta_path = tmp_path / "walk_graph.npz", tmp_path / "walk_graph_meta.json"
    meta = save_graph(graph, poi, lm, stats, npz, meta_path, poi_total=2, landmark_total=1,
                      osm_snapshot="2026-10-19")
    data = load_graph(npz)
    assert sorted(data) == sorted(NPZ_KEYS)  # exactly the contract arrays
    assert data["node_lat"].dtype == np.float32 and data["node_lon"].dtype == np.float32
    assert data["node_flags"].dtype == np.uint8 and data["edge_flags"].dtype == np.uint8
    assert data["edge_u"].dtype == np.int32 and data["edge_v"].dtype == np.int32
    assert data["edge_len_m"].dtype == np.float32
    saved = json.loads(meta_path.read_text(encoding="utf-8"))
    assert saved == meta
    for key in ("osm_snapshot", "bbox", "nodes", "edges", "crossings", "signals",
                "largest_component_share", "poi_nodes", "landmark_nodes", "created_at"):
        assert key in saved, key
    assert saved["bbox"] == [51.03, 71.2, 51.27, 71.65] and saved["osm_snapshot"] == "2026-10-19"
    assert saved["nodes"] == 5 and saved["edges"] == 5
    node5 = int(np.flatnonzero(graph.node_osm_id == 5)[0])
    assert saved["poi_nodes"] == {"osm:node/500": node5}
    assert saved["landmark_nodes"] == {"expo": int(np.flatnonzero(graph.node_osm_id == 1)[0])}
    assert saved["poi_snapped_share"] == 0.5
    assert saved["file_bytes"] == npz.stat().st_size < 15 * 1024 * 1024


def test_fetch_walk_tiles_cache_and_partial(tmp_path: Path) -> None:
    from pipeline.walk_graph import fetch_walk

    queries: list[str] = []

    def fake(query: str, **kwargs: object) -> dict:
        queries.append(query)
        answer = {"elements": [], "qadam": {"endpoint": "e"}}
        if len(queries) == 2:
            answer["remark"] = "runtime error: Query timed out"
        return answer

    files, failed = fetch_walk(raw_dir=tmp_path, query_fn=fake, sleep=lambda s: None)
    assert len(queries) == 9 and len(files) == 9
    assert len(failed) == 1 and "partial answer" in failed[0]
    queries.clear()
    fetch_walk(raw_dir=tmp_path, query_fn=fake, sleep=lambda s: None)
    assert len(queries) == 1  # only the partial tile is fetched again


def test_coverage_meta_marks_a_partial_graph(tmp_path: Path) -> None:
    from pipeline.walk_graph import coverage_meta

    (tmp_path / "osm_walk_tile_4.json").write_text('{"elements": []}', encoding="utf-8")
    (tmp_path / "osm_walk_tile_1.json").write_text(
        '{"elements": [], "qadam": {"partial": true}}', encoding="utf-8")
    cov = coverage_meta(sorted(tmp_path.glob("osm_walk_tile_*.json")))
    assert cov["coverage"] == "partial" and cov["tiles_total"] == 9
    assert cov["tiles_loaded"] == ["walk_tile_1", "walk_tile_4"]
    assert cov["tiles_partial"] == ["walk_tile_1"] and len(cov["tiles_missing"]) == 7
    assert cov["covered_bboxes"][1] == [51.11, 71.35, 51.19, 71.5]  # the centre tile


CROSSINGS = {
    "osm3s": {"timestamp_osm_base": "2026-10-19T08:00:00Z"},
    "elements": [
        n(20, 0, 0), n(21, 0, 1), n(22, 0, 2), n(30, -1, 1), n(31, 1, 1), n(41, 1.5, 1),
        n(42, 2, 1), n(50, 0, 3, highway="traffic_signals", crossing="traffic_signals"),
        n(51, 0, 4), n(60, 0, 5, highway="crossing"), n(61, 0, 6),
        w(200, [20, 21, 22], highway="residential"),
        # the crossing type is only on the way: it goes to node 21 where the way meets the road
        w(201, [30, 21, 31], highway="footway", footway="crossing", crossing="traffic_signals"),
        # no road node: the middle node 41 becomes the crossing; markings=no -> unmarked
        w(202, [31, 41, 42], highway="footway", footway="crossing", crossing="uncontrolled",
          **{"crossing:markings": "no"}),
        w(203, [22, 50, 51], highway="footway"),
        # an untyped highway=crossing node takes the way's type
        w(204, [51, 60, 61], highway="footway", footway="crossing", crossing="unmarked"),
    ],
}


def test_crossing_type_from_way_tags_and_every_crossing_segment_ends_at_one() -> None:
    graph, stats = build_graph([CROSSINGS])
    flags = dict(zip(graph.node_osm_id.tolist(), graph.node_flags.tolist(), strict=True))
    assert flags[21] == NODE_CROSSING | NODE_SIGNALS
    assert flags[41] == NODE_CROSSING | NODE_UNMARKED
    assert flags[50] == NODE_CROSSING | NODE_SIGNALS  # traffic_signals + crossing=traffic_signals
    assert flags[60] == NODE_CROSSING | NODE_UNMARKED
    assert flags[20] == flags[31] == 0
    assert stats["crossings_from_way_tags"] == 3
    for k in np.flatnonzero(graph.edge_flags & EDGE_CROSSING):
        ends = graph.node_flags[graph.edge_u[k]] | graph.node_flags[graph.edge_v[k]]
        assert ends & NODE_CROSSING, k


@pytest.mark.parametrize("payload", [PAYLOAD, CROSSINGS])
def test_edge_length_never_below_straight_line_of_stored_ends(payload: dict) -> None:
    graph, _ = build_graph([payload])
    la, lo = np.radians(graph.node_lat.astype(float)), np.radians(graph.node_lon.astype(float))
    u, v = graph.edge_u, graph.edge_v
    a = np.sin((la[v] - la[u]) / 2) ** 2 + np.cos(la[u]) * np.cos(la[v]) * np.sin(
        (lo[v] - lo[u]) / 2) ** 2
    straight = 2 * 6_371_000.0 * np.arcsin(np.sqrt(a))
    assert (graph.edge_len_m.astype(float) >= straight).all()


def test_meta_has_format_version_and_steps(built, tmp_path: Path) -> None:
    graph, stats = built
    meta = save_graph(graph, {}, {}, stats, tmp_path / "g.npz", tmp_path / "m.json",
                      poi_total=0, landmark_total=0, osm_snapshot="2026-10-19")
    assert meta["format_version"] == 1 and meta["steps"] == 1


MORE_CROSSINGS = {
    "osm3s": {"timestamp_osm_base": "2026-10-19T08:00:00Z"},
    "elements": [
        n(80, 0, 0), n(81, 0, 1), n(82, 2, 0), n(83, 2, 1),
        n(90, 3, 1, highway="crossing", crossing="marked"), n(91, 4, 1), n(92, 5, 1),
        n(93, 4, 2), n(94, 6, 1, highway="crossing", crossing="uncontrolled"), n(95, 7, 1),
        n(96, 8, 1), n(97, 7, 0), n(98, 7, 2),
        w(300, [80, 81], highway="footway"), w(301, [82, 83], highway="footway"),
        # two nodes, no road, no crossing node: a new node in the middle carries the type
        w(302, [81, 83], highway="footway", footway="crossing", crossing="traffic_signals"),
        # 91-92 is the approach of a crossing whose node is 90: no crossing bit there
        w(303, [83, 90, 91, 92], highway="footway", footway="crossing", crossing="marked"),
        w(304, [91, 93], highway="footway"),
        # a second carriageway (road through 95) of a crossing whose mapped node is 94
        w(305, [92, 94, 95, 96], highway="footway", footway="crossing", crossing="uncontrolled"),
        w(306, [97, 95, 98], highway="residential"),
    ],
}


def test_new_middle_node_approach_segments_and_second_carriageway() -> None:
    graph, stats = build_graph([MORE_CROSSINGS])
    ids = graph.node_osm_id.tolist()
    flags = dict(zip(ids, graph.node_flags.tolist(), strict=True))
    new = [i for i in ids if i < 0]
    assert len(new) == 1 and flags[new[0]] == NODE_CROSSING | NODE_SIGNALS
    k = ids.index(new[0])
    assert graph.node_lat[k] == pytest.approx(LAT0 + DLAT, abs=1e-6)  # between 81 and 83
    assert stats["crossing_nodes_added"] == 1
    assert flags[95] == NODE_CROSSING and flags[94] == NODE_CROSSING and flags[91] == 0
    assert graph.edge_flags[edge_index(graph, 91, 92)] == EDGE_FOOTWAY
    assert graph.edge_flags[edge_index(graph, 90, 91)] == EDGE_FOOTWAY | EDGE_CROSSING
    assert stats["crossing_approach_segments"] == 1
    for e in np.flatnonzero(graph.edge_flags & EDGE_CROSSING):
        assert (graph.node_flags[graph.edge_u[e]] | graph.node_flags[graph.edge_v[e]]) & 1, e


def test_meta_license_extent_and_size_check_keeps_previous_graph(
    built, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import pipeline.walk_graph as wg

    graph, stats = built
    npz, meta_path = tmp_path / "g.npz", tmp_path / "m.json"
    meta = save_graph(graph, {}, {}, stats, npz, meta_path, poi_total=0, landmark_total=0,
                      osm_snapshot="2026-10-19")
    assert meta["license"].startswith("ODbL 1.0") and meta["nodes_outside_bbox"] == 0
    s, w, n_, e = meta["nodes_bbox"]
    assert 51.03 <= s <= n_ <= 51.27 and 71.2 <= w <= e <= 71.65
    before = npz.read_bytes()
    monkeypatch.setattr(wg, "MAX_FILE_BYTES", 10)
    with pytest.raises(ValueError):
        save_graph(graph, {}, {}, stats, npz, meta_path, poi_total=0, landmark_total=0,
                   osm_snapshot="2026-10-19")
    assert npz.read_bytes() == before and not list(tmp_path.glob("*.tmp.npz"))


def test_server_timeout_below_the_http_client_timeout() -> None:
    from pipeline.fetch_osm import TIMEOUT_S

    assert f"[timeout:{int(TIMEOUT_S) - 10}]" in walk_query("51.03,71.35,51.11,71.5")
