"""pipeline.walk_coverage on a tiny synthetic graph (no network, no files)."""

from __future__ import annotations

import pytest

from pipeline.walk_coverage import crossing_class, kerb_class, lit_class, measure
from pipeline.walk_graph import (
    EDGE_LIT,
    EDGE_UNLIT,
    NODE_CROSSING,
    NODE_KERB,
    NODE_KERB_HIGH,
    NODE_SIGNALS,
    NODE_UNMARKED,
    build_graph,
)


def node(nid: int, lat: float, lon: float, **tags: str) -> dict:
    return {"type": "node", "id": nid, "lat": lat, "lon": lon, "tags": tags}


def way(wid: int, refs: list[int], **tags: str) -> dict:
    return {"type": "way", "id": wid, "nodes": refs, "tags": tags}


# road 1-2-3-4 (lit), crossed at 2 by the crossing way 10-2-11 (kerbs at both ends), a
# signalised crossing node 3 on the road, steps 4-5 (lit=no, ramp), a crossing way 14-12-13
# over a service road at 12 (untyped, flush kerb on its end node 13)
PAYLOAD = {
    "qadam": {"fetched_at_unix": 1792314269, "chunk": "walk_tile_4"},
    "osm3s": {"timestamp_osm_base": "2026-10-18T10:00:00Z"},
    "elements": [
        node(1, 51.1000, 71.4000),
        node(2, 51.1000, 71.4010),
        node(3, 51.1000, 71.4020, highway="crossing", crossing="traffic_signals"),
        node(4, 51.1000, 71.4030),
        node(5, 51.1005, 71.4030),
        node(10, 51.0998, 71.4010, kerb="lowered"),
        node(11, 51.1002, 71.4010, kerb="raised"),
        node(12, 51.1006, 71.4010, highway="crossing"),
        node(13, 51.1010, 71.4010, kerb="flush"),
        node(14, 51.1004, 71.4010),
        node(20, 51.1006, 71.4000),
        node(21, 51.1006, 71.4020),
        way(100, [1, 2, 3, 4], highway="residential", lit="yes"),
        way(101, [10, 2, 11], highway="footway", footway="crossing", crossing="marked"),
        way(102, [4, 5], highway="steps", lit="no", ramp="yes"),
        way(103, [14, 12, 13], highway="footway", footway="crossing"),
        way(106, [11, 14], highway="footway"),
        way(104, [20, 12, 21], highway="service"),
        way(105, [20, 1], highway="footway"),
    ],
}


@pytest.mark.parametrize(("flags", "expected"), [
    (EDGE_LIT, "yes"), (EDGE_UNLIT, "no"), (0, "unknown"),
])
def test_lit_class_reads_the_npz_bits(flags: int, expected: str) -> None:
    assert lit_class(flags) == expected


@pytest.mark.parametrize(("flags", "tags", "expected"), [
    (NODE_SIGNALS | NODE_UNMARKED, {"crossing": "traffic_signals"}, "signals"),
    (NODE_UNMARKED, {"crossing": "unmarked"}, "unmarked"),
    (0, {"crossing": "uncontrolled"}, "marked"),
    (0, {"crossing:markings": "zebra"}, "marked"),
    (0, {}, "unknown"),
])
def test_crossing_class(flags: int, tags: dict, expected: str) -> None:
    assert crossing_class(flags, tags) == expected


@pytest.mark.parametrize(("flags", "expected"), [
    (NODE_KERB, "lowered"), (NODE_KERB | NODE_KERB_HIGH, "raised"), (0, "unknown"),
])
def test_kerb_class_reads_the_npz_bits(flags: int, expected: str) -> None:
    assert kerb_class(flags) == expected


def test_graph_bits_lit_no_and_kerbs_of_the_crossing_way() -> None:
    graph, stats = build_graph([PAYLOAD])
    flags = dict(zip(graph.node_osm_id.tolist(), graph.node_flags.tolist(), strict=True))
    # lowered on one side, raised on the other: the crossing is "not lowered", one verdict
    assert flags[2] & NODE_CROSSING and flags[2] & NODE_KERB_HIGH and not flags[2] & NODE_KERB
    assert flags[12] & NODE_KERB and not flags[12] & NODE_KERB_HIGH  # flush on its way
    assert not flags[3] & (NODE_KERB | NODE_KERB_HIGH)  # no kerb data
    assert flags[11] & NODE_KERB_HIGH and flags[10] & NODE_KERB  # the kerb nodes themselves
    assert stats["unlit_edges"] == 1 and stats["crossings_kerb_lowered"] == 1
    assert stats["crossings_kerb_raised"] == 1


def test_measure_counts_length_and_crossing_nodes() -> None:
    graph, _ = build_graph([PAYLOAD])
    cov = measure(graph)
    assert cov["edges"] == len(graph.edge_len_m) and cov["crossings_total"] == 3
    lit = cov["lit"]
    assert lit["yes"]["edges"] == 3 and lit["no"]["edges"] == 1  # road split at 2 and 3
    assert sum(item["edges"] for item in lit.values()) == cov["edges"]
    assert sum(item["length_share"] for item in lit.values()) == pytest.approx(1, abs=1e-3)
    assert cov["lit_footways"]["yes"]["edges"] == 0  # the lit road is not a footway
    assert cov["steps"]["edges"] == 1 and cov["steps"]["with_ramp_tags"] == 1
    # node 2 got its type from the crossing way, node 3 has its own, node 12 has none
    assert cov["crossings"]["marked"]["nodes"] == 1
    assert cov["crossings"]["signals"]["nodes"] == 1
    assert cov["crossings"]["unknown"]["nodes"] == 1
    assert {k: v["nodes"] for k, v in cov["kerbs"].items()} == {
        "lowered": 1, "raised": 1, "unknown": 1}
    assert cov["kerb_values"] == {"lowered": 1, "raised": 1, "flush": 1}
