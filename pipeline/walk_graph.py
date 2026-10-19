"""Pedestrian graph of Astana from OSM -> data/processed/walk_graph.npz + walk_graph_meta.json.

    python -m pipeline.walk_graph                 # fetch missing tiles, build, snap, save
    python -m pipeline.walk_graph --skip-fetch    # rebuild from data/raw/osm_walk_tile_*.json

Ways a person may walk (footway incl. sidewalks, path, pedestrian, steps, living_street,
residential, service, unclassified, tertiary, secondary, primary, track, cycleway,
corridor; never motorway/trunk, foot=no or access=private) are split at shared and tagged
nodes; chains of plain nodes are compressed into one edge whose length follows the OSM
line; only the largest connected component is kept; of parallel edges between the same two
nodes the shortest stays. Every POI and landmark is snapped to the nearest graph node
within 150 m.

Format = the DATA -> FEATURES contract, docs/ARCHITECTURE.md section 5:
walk_graph.npz (np.savez_compressed), node number = row in the node arrays:
  node_lat, node_lon float32[N]; node_flags uint8[N] (1 crossing, 2 traffic signals,
  4 lowered kerb, 8 unmarked crossing)
  edge_u, edge_v int32[E]: undirected, every edge once, u != v; edge_len_m float32[E] along
  the OSM line, never shorter than the straight line between the stored (float32) ends;
  edge_flags uint8[E] (1 steps, 2 lit, 4 footway, 8 major road, 16 crossing)
A footway=crossing way gives its own crossing tags to an untyped crossing node, to the node
where it meets a road with cars, or (no such node) to its middle node, a new one for a
two-node way; bit 16 stays only on segments ending at a crossing node, so a crossing
segment always has its crossing node at one end.
walk_graph_meta.json: osm_snapshot, bbox [south, west, north, east], nodes, edges,
  crossings, signals, largest_component_share, poi_nodes {poi_id: node},
  landmark_nodes {landmark_id: node}, created_at (+ reference counters and flag legend).
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import time
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import numpy as np

from app.config import get_settings
from app.hours import ASTANA_TZ
from app.schemas import POI
from pipeline.fetch_osm import (
    ASTANA_BBOX,
    ASTANA_GRID,
    BETWEEN_CHUNKS_S,
    ENDPOINTS,
    TIMEOUT_S,
    OverpassError,
    bbox_tiles,
    is_fresh,
    is_partial_file,
    prefer,
    raw_path,
    run_query,
    save_json,
)
from pipeline.normalize import haversine_m, osm_base

log = logging.getLogger(__name__)

WALKABLE: tuple[str, ...] = (
    "footway", "path", "pedestrian", "steps", "living_street", "residential", "service",
    "unclassified", "tertiary", "secondary", "primary", "track", "cycleway", "corridor",
)
FOOT_INFRA = frozenset({"footway", "path", "pedestrian", "steps", "corridor", "living_street"})
MAJOR_ROADS = frozenset({"primary", "secondary", "tertiary"})
CROSSING_TYPES = frozenset({"traffic_signals", "marked", "zebra", "uncontrolled", "unmarked"})
# roads with cars: where a footway=crossing way meets one, that node is the crossing itself
CAR_ROADS = frozenset(
    {"residential", "service", "unclassified", "tertiary", "secondary", "primary"}
)
KERB_LOW = frozenset({"lowered", "flush"})
# foot=* overrides access=* for pedestrians: access=private + foot=yes is walkable
FOOT_ALLOWED = frozenset({"yes", "designated", "permissive"})

NODE_CROSSING, NODE_SIGNALS, NODE_KERB, NODE_UNMARKED = 1, 2, 4, 8
EDGE_STEPS, EDGE_LIT, EDGE_FOOTWAY, EDGE_MAJOR, EDGE_CROSSING = 1, 2, 4, 8, 16
NODE_FLAGS = {
    "crossing": NODE_CROSSING, "traffic_signals": NODE_SIGNALS, "kerb_lowered": NODE_KERB,
    "unmarked_crossing": NODE_UNMARKED,
}
EDGE_FLAGS = {
    "steps": EDGE_STEPS, "lit": EDGE_LIT, "footway": EDGE_FOOTWAY, "major_road": EDGE_MAJOR,
    "crossing": EDGE_CROSSING,
}
FORMAT_VERSION = 1
NPZ_KEYS = ("node_lat", "node_lon", "node_flags", "edge_u", "edge_v", "edge_len_m", "edge_flags")

SNAP_MAX_M = 150.0
MAX_FILE_BYTES = 20 * 1024 * 1024  # contract limit; the task itself asked for <= 15 MB
# the server gives up before the HTTP client does (fetch_osm.TIMEOUT_S): no answer is lost
WALK_TIMEOUT_S = int(TIMEOUT_S) - 10
LICENSE = "ODbL 1.0, © участники OpenStreetMap"
CHUNK_PREFIX = "walk_tile_"
# access=private ways open to pedestrians (foot=yes...), whole city in one small query; the
# tile query has them too, this file fills in tiles fetched before that
FOOT_OVERRIDE_CHUNK = "walk_foot_override"


# ------------------------------------------------------------------------ fetch


def _ways(highway_filter: str, bbox: str) -> list[str]:
    return [
        f'{highway_filter}["foot"!~"^no$"]["access"!~"^private$"]({bbox});',
        f'{highway_filter}["access"="private"]["foot"~"^({"|".join(sorted(FOOT_ALLOWED))})$"]'
        f"({bbox});",
    ]


def walk_query(bbox: str, timeout_s: int = WALK_TIMEOUT_S) -> str:
    """Walkable ways in a bbox with all their nodes (tags kept: crossings, signals, kerbs)."""
    lines = _ways(f'way["highway"~"^({"|".join(WALKABLE)})$"]', bbox)
    return (
        f"[out:json][timeout:{timeout_s}];\n(\n  " + "\n  ".join(lines) + "\n);\n"
        "out body;\n>;\nout body qt;"
    )


def foot_override_query(bbox: str, timeout_s: int = WALK_TIMEOUT_S) -> str:
    """Only the access=private ways that pedestrians may use (foot=yes/designated/permissive)."""
    line = _ways(f'way["highway"~"^({"|".join(WALKABLE)})$"]', bbox)[1]
    return f"[out:json][timeout:{timeout_s}];\n{line}\nout body;\n>;\nout body qt;"


def walk_chunks() -> list[tuple[str, str]]:
    tiles = bbox_tiles(ASTANA_BBOX, *ASTANA_GRID)
    return [(f"{CHUNK_PREFIX}{i}", f"{s},{w},{n},{e}") for i, (s, w, n, e) in enumerate(tiles)]


def fetch_walk(
    *,
    force: bool = False,
    raw_dir: Path | None = None,
    query_fn: Callable[..., dict] = run_query,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[list[Path], list[str]]:
    """Fetch the walk tiles that have no fresh, complete cache. Returns (files, failures)."""
    done: list[Path] = []
    failed: list[str] = []
    last_good: str | None = None
    requested = False
    s, w, n, e = ASTANA_BBOX
    chunks = [(name, bbox, walk_query) for name, bbox in walk_chunks()]
    chunks.append((FOOT_OVERRIDE_CHUNK, f"{s},{w},{n},{e}", foot_override_query))
    for name, bbox, make_query in chunks:
        path = raw_path(name, raw_dir)
        if not force and is_fresh(path) and not is_partial_file(path):
            done.append(path)
            continue
        if requested:
            sleep(BETWEEN_CHUNKS_S)
        requested = True
        started = time.time()
        try:
            payload = query_fn(make_query(bbox), endpoints=prefer(last_good, ENDPOINTS),
                               check_status=True)
        except OverpassError as exc:
            log.error("walk chunk %s failed: %s", name, exc)
            failed.append(f"{name}: {exc}")
            continue
        last_good = payload.get("qadam", {}).get("endpoint")
        payload.setdefault("qadam", {}).update({
            "chunk": name, "filters": [bbox], "fetched_at_unix": int(started),
            "partial": bool(payload.get("remark")),
        })
        save_json(path, payload)
        done.append(path)
        if payload.get("remark"):
            failed.append(f"{name}: partial answer ({str(payload['remark'])[:120]})")
        log.info("walk chunk %s: %d elements", name, len(payload.get("elements", [])))
    return done, failed


# ------------------------------------------------------------------------ build


@dataclass
class WalkGraph:
    node_lat: np.ndarray
    node_lon: np.ndarray
    node_flags: np.ndarray
    edge_u: np.ndarray
    edge_v: np.ndarray
    edge_len_m: np.ndarray
    edge_flags: np.ndarray
    node_osm_id: np.ndarray  # for tests and debugging only: not written to the npz


def walkable(tags: Mapping[str, str]) -> bool:
    foot = tags.get("foot")
    return (
        tags.get("highway") in WALKABLE
        and foot != "no"
        and (tags.get("access") != "private" or foot in FOOT_ALLOWED)
    )


def crossing_flags_of(tags: Mapping[str, str]) -> int:
    """Crossing type from crossing=* / crossing:markings=* (no tag = an ordinary crossing)."""
    kind = tags.get("crossing")
    if kind is not None and kind not in CROSSING_TYPES:
        return 0  # crossing=no, informal...
    flags = NODE_CROSSING
    if kind == "traffic_signals":
        flags |= NODE_SIGNALS
    if kind == "unmarked" or tags.get("crossing:markings") == "no":
        flags |= NODE_UNMARKED
    return flags


def node_flags_of(tags: Mapping[str, str]) -> int:
    flags = 0
    if tags.get("highway") == "crossing" or tags.get("crossing") in CROSSING_TYPES:
        flags |= crossing_flags_of(tags)
    if tags.get("highway") == "traffic_signals" or tags.get("crossing") == "traffic_signals":
        flags |= NODE_SIGNALS
    if tags.get("kerb") in KERB_LOW:
        flags |= NODE_KERB
    return flags


def edge_flags_of(tags: Mapping[str, str]) -> int:
    highway = tags.get("highway", "")
    flags = 0
    if highway == "steps":
        flags |= EDGE_STEPS
    if tags.get("lit") == "yes":
        flags |= EDGE_LIT
    if highway in FOOT_INFRA or tags.get("foot") == "designated":
        flags |= EDGE_FOOTWAY
    if highway in MAJOR_ROADS:
        flags |= EDGE_MAJOR
    if tags.get("footway") == "crossing":
        flags |= EDGE_CROSSING
    return flags


def build_graph(payloads: Iterable[Mapping]) -> tuple[WalkGraph, dict]:
    """Raw Overpass answers -> compressed graph of the largest connected component."""
    coords: dict[int, tuple[float, float]] = {}
    node_tags: dict[int, Mapping[str, str]] = {}
    ways: dict[int, tuple[list[int], Mapping[str, str]]] = {}
    for payload in sorted(payloads, key=osm_base, reverse=True):  # freshest snapshot first
        for el in payload.get("elements", []):
            if el.get("type") == "node" and el["id"] not in coords:
                coords[el["id"]] = (float(el["lat"]), float(el["lon"]))
                if el.get("tags"):
                    node_tags[el["id"]] = el["tags"]
            elif el.get("type") == "way" and el["id"] not in ways:
                ways[el["id"]] = (list(el.get("nodes") or []), el.get("tags") or {})

    excluded = 0
    kept_ways: list[tuple[list[int], Mapping[str, str]]] = []
    for refs, tags in ways.values():
        if walkable(tags) and len(refs) >= 2:
            kept_ways.append((refs, tags))
        else:
            excluded += 1

    uses: dict[int, int] = defaultdict(int)
    for refs, _ in kept_ways:
        for ref in refs:
            uses[ref] += 1
    node_flags = {r: node_flags_of(node_tags.get(r, {})) for r in uses}
    from_ways = _crossings_from_ways(kept_ways, node_tags, node_flags, coords)
    keep: set[int] = {r for r, c in uses.items() if c > 1}
    keep |= {r for r, f in node_flags.items() if f}
    for refs, _ in kept_ways:
        keep.update((refs[0], refs[-1]))

    # split ways into edges between kept nodes; plain nodes in between only add length
    raw_edges: list[tuple[int, int, float, int]] = []
    missing = approaches = 0
    for refs, tags in kept_ways:
        flags = edge_flags_of(tags)
        start, length, prev = None, 0.0, None
        for ref in refs:
            if ref not in coords:  # node outside the answer: break the chain here
                missing += 1
                start, length, prev = None, 0.0, None
                continue
            if prev is not None:
                length += haversine_m(*coords[prev], *coords[ref])
            if ref in keep:
                if start is not None and start != ref and length > 0:
                    seg = flags
                    # bit 16 only on the segments at the crossing itself: the rest of a long
                    # footway=crossing way is the approach, not one more crossing
                    if seg & EDGE_CROSSING and not (
                        (node_flags.get(start, 0) | node_flags.get(ref, 0)) & NODE_CROSSING
                    ):
                        seg &= ~EDGE_CROSSING
                        approaches += 1
                    raw_edges.append((start, ref, length, seg))
                start, length = ref, 0.0
            prev = ref

    largest, components, total_nodes = _largest_component(raw_edges)
    # every undirected edge once: of parallel ways between the same two nodes keep the shortest
    best: dict[tuple[int, int], tuple[int, int, float, int]] = {}
    in_largest = 0
    for u, v, length, flags in raw_edges:
        if u not in largest:
            continue
        in_largest += 1
        key = (u, v) if u < v else (v, u)
        if key not in best or length < best[key][2]:
            best[key] = (u, v, length, flags)
    edges = [best[k] for k in sorted(best)]
    node_ids = sorted({u for u, *_ in edges} | {v for _, v, *_ in edges})
    index = {osm_id: i for i, osm_id in enumerate(node_ids)}

    node_flag_arr = np.array([node_flags[i] for i in node_ids], dtype=np.uint8)
    edge_flag_arr = np.array([e[3] for e in edges], dtype=np.uint8)
    lat = np.array([coords[i][0] for i in node_ids], dtype=np.float32)
    lon = np.array([coords[i][1] for i in node_ids], dtype=np.float32)
    edge_u = np.array([index[e[0]] for e in edges], dtype=np.int32)
    edge_v = np.array([index[e[1]] for e in edges], dtype=np.int32)
    graph = WalkGraph(
        node_lat=lat,
        node_lon=lon,
        node_flags=node_flag_arr,
        edge_u=edge_u,
        edge_v=edge_v,
        edge_len_m=_not_shorter_than_straight(
            np.array([e[2] for e in edges], dtype=np.float64), lat, lon, edge_u, edge_v
        ),
        edge_flags=edge_flag_arr,
        node_osm_id=np.array(node_ids, dtype=np.int64),
    )
    stats = {
        "ways_used": len(kept_ways),
        "ways_excluded": excluded,
        "nodes_missing_coords": missing,
        "components_total": components,
        "components_dropped": components - 1 if components else 0,
        "largest_component_share": round(len(largest) / total_nodes, 4) if total_nodes else 0.0,
        "parallel_edges_merged": in_largest - len(edges),
        "crossings": int(np.count_nonzero(node_flag_arr & NODE_CROSSING)),
        "signals": int(np.count_nonzero(node_flag_arr & NODE_SIGNALS)),
        "unmarked_crossings": int(np.count_nonzero(node_flag_arr & NODE_UNMARKED)),
        "crossings_from_way_tags": sum(1 for r in from_ways if r in index),
        "crossing_nodes_added": sum(1 for r in from_ways if r < 0 and r in index),
        "crossing_approach_segments": approaches,
        "kerbs_lowered": int(np.count_nonzero(node_flag_arr & NODE_KERB)),
        "crossing_edges": int(np.count_nonzero(edge_flag_arr & EDGE_CROSSING)),
        "steps_edges": int(np.count_nonzero(edge_flag_arr & EDGE_STEPS)),
        "lit_edges": int(np.count_nonzero(edge_flag_arr & EDGE_LIT)),
        "total_length_km": round(float(graph.edge_len_m.sum()) / 1000, 1),
    }
    return graph, stats


def _crossings_from_ways(
    kept_ways: Sequence[tuple[list[int], Mapping[str, str]]],
    node_tags: Mapping[int, Mapping[str, str]],
    node_flags: dict[int, int],
    coords: dict[int, tuple[float, float]],
) -> set[int]:
    """Crossing tags of footway=crossing ways onto nodes (updates node_flags in place).

    OSM puts crossing=* both on the crossing node and on the crossing way; when the node is
    missing or untyped, the way's type is used, so a crossing segment always ends at a
    crossing node and a signalised crossing is not taken for an unmarked one:
    - an untyped crossing node (highway=crossing alone) takes the way's type;
    - an inner node where the way meets a road with cars becomes a crossing of that type;
    - no such node: the middle node of the way, or a new node (negative id) in the middle
      of a two-node way, inserted into its refs.
    Returns the nodes that got flags this way."""
    road_nodes = {r for refs, tags in kept_ways if tags.get("highway") in CAR_ROADS for r in refs}
    changed: set[int] = set()
    synthetic = 0
    for refs, tags in kept_ways:
        if tags.get("footway") != "crossing" or not (flags := crossing_flags_of(tags)):
            continue
        at = [r for r in refs if node_flags.get(r, 0) & NODE_CROSSING
              and "crossing" not in node_tags.get(r, {})]
        # inner nodes only: a crossing way that ends on a road does not cross it there
        at += [r for r in refs[1:-1]
               if r in road_nodes and not node_flags.get(r, 0) & NODE_CROSSING]
        if not at and not any(node_flags.get(r, 0) & NODE_CROSSING for r in refs):
            if len(refs) > 2:
                at = [refs[len(refs) // 2]]
            elif refs[0] in coords and refs[1] in coords:
                synthetic -= 1
                (lat1, lon1), (lat2, lon2) = coords[refs[0]], coords[refs[1]]
                coords[synthetic] = ((lat1 + lat2) / 2, (lon1 + lon2) / 2)
                refs.insert(1, synthetic)
                at = [synthetic]
        for r in at:
            if node_flags.get(r, 0) | flags != node_flags.get(r, 0):
                node_flags[r] = node_flags.get(r, 0) | flags
                changed.add(r)
    return changed


def _not_shorter_than_straight(
    length: np.ndarray, lat: np.ndarray, lon: np.ndarray, u: np.ndarray, v: np.ndarray
) -> np.ndarray:
    """float32 lengths >= the haversine between the stored float32 ends (+0.01%), so a
    straight-line A* bound never overestimates."""
    la, lo = np.radians(lat.astype(np.float64)), np.radians(lon.astype(np.float64))
    a = (np.sin((la[v] - la[u]) / 2) ** 2
         + np.cos(la[u]) * np.cos(la[v]) * np.sin((lo[v] - lo[u]) / 2) ** 2)
    straight = 2 * 6_371_000.0 * np.arcsin(np.sqrt(a)) * 1.0001
    out = np.maximum(length, straight).astype(np.float32)
    short = out.astype(np.float64) < straight
    out[short] = np.nextafter(out[short], np.float32(np.inf))
    return out


def _largest_component(edges: Sequence[tuple]) -> tuple[set[int], int, int]:
    """(nodes of the largest component, number of components, number of nodes)."""
    parent: dict[int, int] = {}

    def find(x: int) -> int:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for u, v, *_ in edges:
        ru, rv = find(u), find(v)
        if ru != rv:
            parent[ru] = rv
    groups: dict[int, set[int]] = defaultdict(set)
    for node in list(parent):
        groups[find(node)].add(node)
    if not groups:
        return set(), 0, 0
    return max(groups.values(), key=len), len(groups), len(parent)


# ------------------------------------------------------------------------- snap


def snap_points(
    graph: WalkGraph, points: Iterable[tuple[str, float, float]], max_m: float = SNAP_MAX_M
) -> dict[str, tuple[int, float]]:
    """{id: (node, distance_m)} for points with a graph node within max_m (grid of max_m)."""
    if not len(graph.node_lat):
        return {}
    lat0 = float(np.mean(graph.node_lat))
    m_lat = 111_320.0
    m_lon = 111_320.0 * math.cos(math.radians(lat0))
    cells: dict[tuple[int, int], list[int]] = defaultdict(list)
    for i, (lat, lon) in enumerate(zip(graph.node_lat, graph.node_lon, strict=True)):
        cells[(int(float(lat) * m_lat // max_m), int(float(lon) * m_lon // max_m))].append(i)
    out: dict[str, tuple[int, float]] = {}
    for pid, lat, lon in points:
        cy, cx = int(lat * m_lat // max_m), int(lon * m_lon // max_m)
        best: tuple[float, int] | None = None
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                for i in cells.get((cy + dy, cx + dx), ()):
                    d = haversine_m(lat, lon, float(graph.node_lat[i]), float(graph.node_lon[i]))
                    if d <= max_m and (best is None or d < best[0]):
                        best = (d, i)
        if best is not None:
            out[pid] = (best[1], round(best[0], 1))
    return out


# ------------------------------------------------------------------------- save


def save_graph(
    graph: WalkGraph,
    poi_snaps: Mapping[str, tuple[int, float]],
    landmark_snaps: Mapping[str, tuple[int, float]],
    stats: Mapping,
    npz_path: Path,
    meta_path: Path,
    *,
    poi_total: int,
    landmark_total: int,
    osm_snapshot: str,
    extra_meta: Mapping | None = None,
) -> dict:
    npz_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = npz_path.with_suffix(".tmp.npz")
    np.savez_compressed(tmp, **{key: getattr(graph, key) for key in NPZ_KEYS})
    size = tmp.stat().st_size
    if size > MAX_FILE_BYTES:  # checked before the replace: the previous graph stays
        tmp.unlink()
        raise ValueError(f"walk_graph.npz would be {size / 1e6:.1f} MB > 20 MB")
    tmp.replace(npz_path)
    dists = [d for _, d in poi_snaps.values()]
    meta = {
        # contract keys (docs/ARCHITECTURE.md section 5)
        "format_version": FORMAT_VERSION,
        "osm_snapshot": osm_snapshot,
        "bbox": list(ASTANA_BBOX),
        "nodes": int(len(graph.node_lat)),
        "edges": int(len(graph.edge_u)),
        "crossings": stats["crossings"],
        "signals": stats["signals"],
        "largest_component_share": stats["largest_component_share"],
        "poi_nodes": {pid: node for pid, (node, _) in sorted(poi_snaps.items())},
        "landmark_nodes": {lid: node for lid, (node, _) in sorted(landmark_snaps.items())},
        "created_at": datetime.now(ASTANA_TZ).isoformat(timespec="seconds"),
        # reference
        **{k: v for k, v in stats.items() if k not in {"crossings", "signals",
                                                      "largest_component_share"}},
        "steps": stats.get("steps_edges", 0),  # stair segments (edge bit 1)
        "poi_total": poi_total,
        "poi_snapped": len(poi_snaps),
        "poi_snapped_share": round(len(poi_snaps) / poi_total, 3) if poi_total else 0.0,
        "poi_snap_median_m": float(np.median(dists)) if dists else None,
        "landmarks_total": landmark_total,
        "landmarks_snapped": len(landmark_snaps),
        "snap_max_m": int(SNAP_MAX_M),
        "highways": list(WALKABLE),
        "node_flag_bits": NODE_FLAGS,
        "edge_flag_bits": EDGE_FLAGS,
        "file_bytes": size,
        "license": LICENSE,
        **nodes_extent(graph),
        **dict(extra_meta or {}),
    }
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    return meta


def nodes_extent(graph: WalkGraph) -> dict:
    """Where the nodes really are: ways crossing the bbox are kept whole, so their far ends
    (outskirt roads, the airport south of 51.03) may lie outside `bbox`."""
    if not len(graph.node_lat):
        return {"nodes_bbox": None, "nodes_outside_bbox": 0}
    lat, lon = graph.node_lat.astype(np.float64), graph.node_lon.astype(np.float64)
    s, w, n, e = ASTANA_BBOX
    outside = ~((lat >= s) & (lat <= n) & (lon >= w) & (lon <= e))
    return {
        "nodes_bbox": [round(float(lat.min()), 5), round(float(lon.min()), 5),
                       round(float(lat.max()), 5), round(float(lon.max()), 5)],
        "nodes_outside_bbox": int(np.count_nonzero(outside)),
    }


def load_graph(path: Path) -> dict[str, np.ndarray]:
    with np.load(path, allow_pickle=False) as data:
        return {key: data[key] for key in data.files}


# -------------------------------------------------------------------------- run


def walk_files(raw_dir: Path) -> list[Path]:
    """The city tiles plus the foot-override file, if fetched."""
    extra = raw_dir / f"osm_{FOOT_OVERRIDE_CHUNK}.json"
    return sorted(raw_dir.glob(f"osm_{CHUNK_PREFIX}*.json")) + ([extra] if extra.exists() else [])


def coverage_meta(paths: Sequence[Path]) -> dict:
    """Which city tiles the graph is built from: an honest label for a partial graph."""
    by_name = {p.stem.removeprefix("osm_"): p for p in paths}
    loaded, partial, missing, boxes = [], [], [], []
    for name, bbox in walk_chunks():
        if name not in by_name:
            missing.append(name)
            continue
        loaded.append(name)
        boxes.append([float(x) for x in bbox.split(",")])
        if is_partial_file(by_name[name]):
            partial.append(name)
    return {
        "coverage": "full" if not missing and not partial else "partial",
        "tiles_total": len(walk_chunks()),
        "tiles_loaded": loaded,
        "tiles_missing": missing,
        "tiles_partial": partial,
        "covered_bboxes": boxes,  # [south, west, north, east] of each loaded tile
    }


def run(*, skip_fetch: bool = False, force: bool = False) -> dict:
    settings = get_settings()
    raw_dir = settings.data_dir / "raw"
    failed: list[str] = []
    if not skip_fetch:
        _, failed = fetch_walk(force=force, raw_dir=raw_dir)
    paths = walk_files(raw_dir)
    if not paths:
        raise SystemExit("no walk tiles in data/raw: run without --skip-fetch first")
    payloads = [json.loads(p.read_text(encoding="utf-8")) for p in paths]
    graph, stats = build_graph(payloads)

    with settings.poi_path.open(encoding="utf-8") as fh:
        pois = [POI.model_validate_json(line) for line in fh if line.strip()]
    landmarks = json.loads(settings.landmarks_path.read_text(encoding="utf-8"))
    poi_snaps = snap_points(graph, [(p.id, p.geo.lat, p.geo.lon) for p in pois])
    landmark_snaps = snap_points(
        graph, [(lm["id"], lm["geo"]["lat"], lm["geo"]["lon"]) for lm in landmarks]
    )
    snapshots = sorted({osm_base(p)[:10] for p in payloads if osm_base(p)})
    coverage = coverage_meta(paths)
    # POI outside the loaded tiles cannot snap: count the share among the covered ones too
    covered = [p for p in pois if any(s <= p.geo.lat <= n and w <= p.geo.lon <= e
                                      for s, w, n, e in coverage["covered_bboxes"])]
    coverage["poi_in_coverage"] = len(covered)
    coverage["poi_snapped_share_in_coverage"] = (
        round(sum(1 for p in covered if p.id in poi_snaps) / len(covered), 3) if covered else 0.0
    )
    out_dir = settings.poi_path.parent
    return save_graph(
        graph, poi_snaps, landmark_snaps, stats,
        out_dir / "walk_graph.npz", out_dir / "walk_graph_meta.json",
        poi_total=len(pois), landmark_total=len(landmarks),
        osm_snapshot=snapshots[0] if snapshots else "",  # the oldest tile, to be safe
        extra_meta={"osm_snapshots": snapshots, **coverage, "tiles_failed": failed},
    )


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--skip-fetch", action="store_true", help="use data/raw only")
    parser.add_argument("--force", action="store_true", help="ignore the 24 h raw cache")
    args = parser.parse_args(argv)
    logging.basicConfig(level=get_settings().log_level, format="%(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    meta = run(skip_fetch=args.skip_fetch, force=args.force)
    keys = ("nodes", "edges", "crossings", "signals", "steps_edges", "largest_component_share",
            "poi_snapped_share", "landmarks_snapped", "file_bytes", "coverage", "tiles_missing",
            "tiles_failed")
    print(json.dumps({k: meta[k] for k in keys}, ensure_ascii=False))


if __name__ == "__main__":
    main()
