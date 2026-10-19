"""Walking routes on the OSM walk graph. Owner: T91 (wave 5, session D:/core, feat/walkgraph).

Graph file: data/processed/walk_graph.npz + walk_graph_meta.json, format in
docs/ARCHITECTURE.md §5 (built by DATA, pipeline/walk_graph.py). Only numpy and heapq.

- The graph is loaded once (cached by path and mtime); any problem -> None, the caller keeps
  straight segments (path_source="straight").
- A point is bound to a node within SNAP_MAX_M: POIs and landmarks via poi_nodes /
  landmark_nodes from the meta, anything else via a grid index.
- A* by time: length / 1.25 m/s; stairs x2, big road x1.3 (pavements and footways win),
  crossing +20 s (traffic lights), +10 s (marked), +40 s (unmarked). Modes: wheelchair —
  no stairs, unmarked crossing +60 s; night — edges without the lit bit x1.1 (in OSM
  Astana lit is tagged on ~3% of edges: no bit means «unknown», not «dark», so a known-lit
  way is only a mild plus); with kids — unmarked +60 s.
  The heuristic is the straight line at the same pace: every real cost is at least that.
- These weights only choose the path. Minutes shown to the user are the length at 75 m/min:
  walk_min = ceil(length / 75), minimum 1 (docs/ARCHITECTURE.md §5).
- One route (all its legs) gets ROUTE_LIMIT_S; past it the search gives up (None).
- A leg falls back to the straight segment (minutes x1.3, as before) when the graph only
  adds its binding links (straight <= link_a + link_b: both points hang on the same far
  node) or makes an absurd detour (> DETOUR_MAX_X x straight + DETOUR_MAX_ADD_M). Review
  on the real graph: «Chaplin 71 м · 9 мин», Хан Шатыр -> Fame City (3 м) = 165 м.
"""

from __future__ import annotations

import heapq
import json
import logging
import math
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from app.geo import haversine_m, walk_minutes
from app.schemas import GeoPoint

log = logging.getLogger(__name__)

FORMAT_VERSION = 1
SPEED_M_S = 1.25  # 4.5 km/h, the pace of app.geo
WALK_M_PER_MIN = 75.0
SNAP_MAX_M = 150.0
ROUTE_LIMIT_S = 0.3

STAIRS_X = 2.0
MAJOR_ROAD_X = 1.3
UNLIT_NIGHT_X = 1.1  # ARCHITECTURE §5: at most x1.1, or routes chase the rare lit=yes ways
CROSSING_S: dict[str, float] = {"signals": 20.0, "marked": 10.0, "unmarked": 40.0}
UNMARKED_CAREFUL_S = 60.0  # in a wheelchair or with kids
DETOUR_MAX_X = 3.0  # graph longer than 3 x straight + 200 m -> the straight segment
DETOUR_MAX_ADD_M = 200.0

NODE_CROSSING, NODE_SIGNALS, NODE_LOWERED_KERB, NODE_UNMARKED = 1, 2, 4, 8
EDGE_STEPS, EDGE_LIT, EDGE_FOOTWAY, EDGE_MAJOR, EDGE_CROSSING = 1, 2, 4, 8, 16

_M_PER_DEG = 111_320.0
_ARRAYS = ("node_lat", "node_lon", "node_flags", "edge_u", "edge_v", "edge_len_m", "edge_flags")
_HEURISTIC_SAFETY = 0.99  # equirectangular distance may be a hair longer than the real one
_CHECK_EVERY = 256  # heap pops between deadline checks
_INF = math.inf


class GraphError(ValueError):
    """The graph file does not follow docs/ARCHITECTURE.md §5."""


class _Timeout(Exception):
    pass


@dataclass(frozen=True)
class Mode:
    wheelchair: bool = False
    night: bool = False
    kids: bool = False


WALK = Mode()  # plain walking, no special needs


@dataclass(frozen=True)
class Crossing:
    lat: float
    lon: float
    kind: str  # "signals" | "marked" | "unmarked" (RouteCrossing.kind)


@dataclass(frozen=True)
class GraphPath:
    """One leg: start point -> its node -> ... -> end node -> end point."""

    points: list[tuple[float, float]]  # (lat, lon)
    length_m: float  # along the graph, plus the short straight links to the two points
    cost_s: float  # search cost with penalties (not shown to the user)
    crossings: list[Crossing]
    steps: int  # stair edges on the path
    straight: bool = False  # a straight segment instead of the graph (see the module doc)

    @property
    def walk_min(self) -> int:
        if self.straight:  # the same rule as a straight route: x1.3 detour factor
            return walk_minutes(self.length_m)
        return max(1, math.ceil(self.length_m / WALK_M_PER_MIN - 1e-9))


def graph_beats_straight(direct_m: float, graph_m: float, links_m: float) -> bool:
    """Use the graph length only when it is a real path, not a binding artefact or a detour."""
    if direct_m <= links_m:  # both points hang on far nodes: the graph adds only the links
        return False
    return graph_m <= DETOUR_MAX_X * direct_m + DETOUR_MAX_ADD_M


def straight_path(a: GeoPoint, b: GeoPoint) -> GraphPath:
    direct = haversine_m(a, b)
    return GraphPath(points=[(a.lat, a.lon), (b.lat, b.lon)], length_m=direct,
                     cost_s=direct / SPEED_M_S, crossings=[], steps=0, straight=True)


def crossing_kind(flags: int) -> str | None:
    """bit 2 -> signals, else bit 8 -> unmarked, else bit 1 -> marked (ARCHITECTURE §5)."""
    if flags & NODE_SIGNALS:
        return "signals"
    if flags & NODE_UNMARKED:
        return "unmarked"
    if flags & NODE_CROSSING:
        return "marked"
    return None


def _crossing_penalty(kind: str | None, mode: Mode) -> float:
    if kind is None:
        return 0.0
    if kind == "unmarked" and (mode.wheelchair or mode.kids):
        return UNMARKED_CAREFUL_S
    return CROSSING_S[kind]


class WalkGraph:
    """Arrays of docs/ARCHITECTURE.md §5 plus a CSR adjacency and a grid index."""

    def __init__(self, arrays: dict[str, np.ndarray], meta: dict[str, object]) -> None:
        missing = [name for name in _ARRAYS if name not in arrays]
        if missing:
            raise GraphError(f"missing arrays: {missing}")
        lat = np.asarray(arrays["node_lat"], dtype=np.float64)
        lon = np.asarray(arrays["node_lon"], dtype=np.float64)
        n_flags = np.asarray(arrays["node_flags"]).astype(np.int64)
        eu = np.asarray(arrays["edge_u"]).astype(np.int64)
        ev = np.asarray(arrays["edge_v"]).astype(np.int64)
        e_len = np.asarray(arrays["edge_len_m"], dtype=np.float64)
        e_flags = np.asarray(arrays["edge_flags"]).astype(np.int64)
        n, m = len(lat), len(eu)
        if not (len(lon) == len(n_flags) == n and len(ev) == len(e_len) == len(e_flags) == m):
            raise GraphError("array lengths do not match")
        if m and (eu.min() < 0 or ev.min() < 0 or max(eu.max(), ev.max()) >= n):
            raise GraphError("edge end outside the node arrays")
        if not (np.isfinite(lat).all() and np.isfinite(lon).all() and np.isfinite(e_len).all()):
            raise GraphError("non-finite coordinates or lengths")
        if m and e_len.min() < 0:
            raise GraphError("negative edge length")

        self.node_lat, self.node_lon = lat, lon
        self.meta = meta
        self._lat, self._lon = lat.tolist(), lon.tolist()
        self._n_flags = n_flags.tolist()
        self._e_len, self._e_flags = e_len.tolist(), e_flags.tolist()
        self._eu, self._ev = eu.tolist(), ev.tolist()
        self._kind = [crossing_kind(f) for f in self._n_flags]
        # a crossing segment with no crossing node at either end still is a crossing: we do not
        # know its kind, so it counts as unmarked (the cautious choice)
        self._seg_crossing = [
            bool(self._e_flags[e] & EDGE_CROSSING)
            and self._kind[self._eu[e]] is None
            and self._kind[self._ev[e]] is None
            for e in range(m)
        ]

        src = np.concatenate([eu, ev])
        dst = np.concatenate([ev, eu])
        eid = np.concatenate([np.arange(m), np.arange(m)])
        order = np.argsort(src, kind="stable")
        self._adj_start = np.searchsorted(src[order], np.arange(n + 1)).tolist()
        self._adj_nbr = dst[order].tolist()
        self._adj_edge = eid[order].tolist()

        lat0 = float(lat.mean()) if n else 0.0
        self._dlat = SNAP_MAX_M / _M_PER_DEG
        self._dlon = SNAP_MAX_M / (_M_PER_DEG * max(0.1, math.cos(math.radians(lat0))))
        self._grid: dict[tuple[int, int], list[int]] = {}
        if n:
            ki = np.floor(lat / self._dlat).astype(np.int64)
            kj = np.floor(lon / self._dlon).astype(np.int64)
            for i, key in enumerate(zip(ki.tolist(), kj.tolist(), strict=True)):
                self._grid.setdefault(key, []).append(i)

        self.poi_nodes = self._node_map(meta.get("poi_nodes"), n)
        self.landmark_nodes = self._node_map(meta.get("landmark_nodes"), n)
        self._costs_by_mode: dict[Mode, tuple[list[float], list[float]]] = {}

    @staticmethod
    def _node_map(raw: object, n: int) -> dict[str, int]:
        if not isinstance(raw, dict):
            return {}
        return {
            str(k): int(v) for k, v in raw.items() if isinstance(v, int) and 0 <= v < n
        }

    # ------------------------------------------------------------ binding

    def point(self, node: int) -> GeoPoint:
        return GeoPoint(lat=self._lat[node], lon=self._lon[node])

    def nearest_node(self, point: GeoPoint, max_m: float = SNAP_MAX_M) -> int | None:
        ki = math.floor(point.lat / self._dlat)
        kj = math.floor(point.lon / self._dlon)
        best, best_d = None, max_m
        for di in (-1, 0, 1):
            for dj in (-1, 0, 1):
                for node in self._grid.get((ki + di, kj + dj), ()):
                    d = haversine_m(point, self.point(node))
                    if d <= best_d:
                        best, best_d = node, d
        return best

    def _bind(self, point: GeoPoint, poi: str | None, landmark: str | None) -> int | None:
        for mapping, key in ((self.poi_nodes, poi), (self.landmark_nodes, landmark)):
            node = mapping.get(key) if key else None
            if node is not None and haversine_m(point, self.point(node)) <= SNAP_MAX_M:
                return node
        return self.nearest_node(point)

    # ------------------------------------------------------------ costs

    def _costs(self, mode: Mode) -> tuple[list[float], list[float]]:
        """(edge cost in s, cost of entering each node in s) for this mode; cached."""
        cached = self._costs_by_mode.get(mode)
        if cached is not None:
            return cached
        e_flags = np.asarray(self._e_flags, dtype=np.int64)
        cost = np.asarray(self._e_len, dtype=np.float64) / SPEED_M_S
        cost = np.where(e_flags & EDGE_STEPS, cost * STAIRS_X, cost)
        cost = np.where(e_flags & EDGE_MAJOR, cost * MAJOR_ROAD_X, cost)
        if mode.night:
            cost = np.where(e_flags & EDGE_LIT, cost, cost * UNLIT_NIGHT_X)
        seg = np.asarray(self._seg_crossing, dtype=bool)
        cost = np.where(seg, cost + _crossing_penalty("unmarked", mode), cost)
        if mode.wheelchair:
            cost = np.where(e_flags & EDGE_STEPS, _INF, cost)
        node_cost = [_crossing_penalty(kind, mode) for kind in self._kind]
        result = (cost.tolist(), node_cost)
        self._costs_by_mode[mode] = result
        return result

    # ------------------------------------------------------------ search

    def _search(
        self, start: int, goal: int, mode: Mode, deadline: float
    ) -> tuple[list[int], list[int], float] | None:
        """A* by time. (nodes, edges, cost) or None when there is no path."""
        if start == goal:
            return [start], [], 0.0
        edge_cost, node_cost = self._costs(mode)
        lat, lon = self._lat, self._lon
        adj_start, adj_nbr, adj_edge = self._adj_start, self._adj_nbr, self._adj_edge
        lat_t, lon_t = lat[goal], lon[goal]
        ky = _M_PER_DEG / SPEED_M_S * _HEURISTIC_SAFETY
        kx = ky * math.cos(math.radians(lat_t))

        def h(v: int) -> float:
            dx, dy = (lon[v] - lon_t) * kx, (lat[v] - lat_t) * ky
            return math.sqrt(dx * dx + dy * dy)

        best = {start: 0.0}
        prev: dict[int, tuple[int, int]] = {}
        closed: set[int] = set()
        heap = [(h(start), 0.0, start)]
        pops = 0
        while heap:
            _, g, v = heapq.heappop(heap)
            if v in closed:
                continue
            if v == goal:
                return self._unwind(start, goal, prev) + (g,)
            closed.add(v)
            pops += 1
            if pops % _CHECK_EVERY == 0 and time.perf_counter() > deadline:
                raise _Timeout
            for k in range(adj_start[v], adj_start[v + 1]):
                w = adj_nbr[k]
                if w in closed:
                    continue
                e = adj_edge[k]
                ng = g + edge_cost[e] + node_cost[w]
                if ng < best.get(w, _INF):
                    best[w] = ng
                    prev[w] = (v, e)
                    heapq.heappush(heap, (ng + h(w), ng, w))
        return None

    @staticmethod
    def _unwind(
        start: int, goal: int, prev: dict[int, tuple[int, int]]
    ) -> tuple[list[int], list[int]]:
        nodes, edges = [goal], []
        while nodes[-1] != start:
            v, e = prev[nodes[-1]]
            nodes.append(v)
            edges.append(e)
        return nodes[::-1], edges[::-1]

    def _path(
        self, a: GeoPoint, b: GeoPoint, na: int, nb: int, found: tuple[list[int], list[int], float]
    ) -> GraphPath:
        nodes, edges, cost = found
        link_a = haversine_m(a, self.point(na))
        link_b = haversine_m(self.point(nb), b)
        crossings: list[Crossing] = []

        def add(lat: float, lon: float, kind: str | None) -> None:
            if kind and not (crossings and crossings[-1].lat == lat and crossings[-1].lon == lon):
                crossings.append(Crossing(lat=lat, lon=lon, kind=kind))

        add(self._lat[nodes[0]], self._lon[nodes[0]], self._kind[nodes[0]])
        for e, node in zip(edges, nodes[1:], strict=True):
            if self._seg_crossing[e]:
                u, v = self._eu[e], self._ev[e]
                add((self._lat[u] + self._lat[v]) / 2, (self._lon[u] + self._lon[v]) / 2,
                    "unmarked")
            add(self._lat[node], self._lon[node], self._kind[node])
        return GraphPath(
            points=[(a.lat, a.lon), *((self._lat[n], self._lon[n]) for n in nodes), (b.lat, b.lon)],
            length_m=link_a + sum(self._e_len[e] for e in edges) + link_b,
            cost_s=cost + (link_a + link_b) / SPEED_M_S,
            crossings=crossings,
            steps=sum(1 for e in edges if self._e_flags[e] & EDGE_STEPS),
        )

    def _leg(
        self,
        a: GeoPoint,
        b: GeoPoint,
        mode: Mode,
        deadline: float,
        a_ref: tuple[str | None, str | None],
        b_ref: tuple[str | None, str | None],
    ) -> GraphPath | None:
        na, nb = self._bind(a, *a_ref), self._bind(b, *b_ref)
        if na is None or nb is None:
            return None
        direct = haversine_m(a, b)
        links = haversine_m(a, self.point(na)) + haversine_m(self.point(nb), b)
        if direct <= links:  # no need to search: the graph would only add the two links
            return straight_path(a, b)
        found = self._search(na, nb, mode, deadline)
        if found is None:
            return None
        path = self._path(a, b, na, nb, found)
        return path if graph_beats_straight(direct, path.length_m, links) else straight_path(a, b)

    # ------------------------------------------------------------ public

    def route(
        self,
        a: GeoPoint,
        b: GeoPoint,
        mode: Mode = WALK,
        *,
        a_poi: str | None = None,
        a_landmark: str | None = None,
        b_poi: str | None = None,
        b_landmark: str | None = None,
        limit_s: float = ROUTE_LIMIT_S,
    ) -> GraphPath | None:
        """Shortest-time leg a -> b, or None (no binding, no path, over the time limit)."""
        legs = self.route_through(
            [a, b], mode, pois=[a_poi, b_poi], landmarks=[a_landmark, b_landmark],
            limit_s=limit_s,
        )
        return legs[0] if legs else None

    def route_through(
        self,
        points: list[GeoPoint],
        mode: Mode = WALK,
        *,
        pois: list[str | None] | None = None,
        landmarks: list[str | None] | None = None,
        limit_s: float = ROUTE_LIMIT_S,
    ) -> list[GraphPath] | None:
        """Legs through consecutive points under ONE deadline; None if any leg fails."""
        pois = pois or [None] * len(points)
        landmarks = landmarks or [None] * len(points)
        deadline = time.perf_counter() + limit_s
        legs: list[GraphPath] = []
        try:
            if time.perf_counter() > deadline:
                raise _Timeout
            for i in range(len(points) - 1):
                leg = self._leg(points[i], points[i + 1], mode, deadline,
                                (pois[i], landmarks[i]), (pois[i + 1], landmarks[i + 1]))
                if leg is None:
                    return None
                legs.append(leg)
        except _Timeout:
            log.warning("walk graph: route over %.0f ms, straight segments", limit_s * 1000)
            return None
        return legs

    def lengths_from(
        self,
        origin: GeoPoint,
        targets: dict[str, GeoPoint],
        mode: Mode = WALK,
        *,
        origin_landmark: str | None = None,
        limit_s: float = ROUTE_LIMIT_S,
    ) -> dict[str, float]:
        """Walking length (m) from origin to each target (keys are POI ids) in one search.

        Targets that cannot be bound or reached are left out, and so are those where the graph
        is only a binding artefact or an absurd detour (graph_beats_straight): the caller keeps
        the straight estimate for them. Over the time limit -> {}.
        """
        start = self._bind(origin, None, origin_landmark)
        if start is None or not targets:
            return {}
        goal_of: dict[int, list[str]] = {}
        for key, point in targets.items():
            node = self._bind(point, key, None)
            if node is not None:
                goal_of.setdefault(node, []).append(key)
        if not goal_of:
            return {}
        edge_cost, node_cost = self._costs(mode)
        deadline = time.perf_counter() + limit_s
        link0 = haversine_m(origin, self.point(start))
        best = {start: 0.0}
        length = {start: 0.0}
        closed: set[int] = set()
        heap = [(0.0, start)]
        left = set(goal_of)
        out: dict[str, float] = {}
        pops = 0
        while heap and left:
            g, v = heapq.heappop(heap)
            if v in closed:
                continue
            closed.add(v)
            if v in left:
                left.discard(v)
                for key in goal_of[v]:
                    link_t = haversine_m(self.point(v), targets[key])
                    metres = link0 + length[v] + link_t
                    if graph_beats_straight(haversine_m(origin, targets[key]), metres,
                                            link0 + link_t):
                        out[key] = metres
            pops += 1
            if pops % _CHECK_EVERY == 0 and time.perf_counter() > deadline:
                log.warning("walk graph: card times over %.0f ms, straight", limit_s * 1000)
                return {}
            for k in range(self._adj_start[v], self._adj_start[v + 1]):
                w = self._adj_nbr[k]
                if w in closed:
                    continue
                e = self._adj_edge[k]
                ng = g + edge_cost[e] + node_cost[w]
                if ng < best.get(w, _INF):
                    best[w] = ng
                    length[w] = length[v] + self._e_len[e]
                    heapq.heappush(heap, (ng, w))
        return out


# ---------------------------------------------------------------- loading

_CACHE: dict[str, tuple[int, WalkGraph | None]] = {}


def load_graph(path: Path | None, meta_path: Path | None = None) -> WalkGraph | None:
    """The graph at `path`, loaded once per file version (mtime); None if missing or broken."""
    if path is None or not path.is_file():
        return None
    try:
        mtime = path.stat().st_mtime_ns
    except OSError:
        return None
    cached = _CACHE.get(str(path))
    if cached is not None and cached[0] == mtime:
        return cached[1]
    graph: WalkGraph | None = None
    try:
        meta_file = meta_path or path.with_name("walk_graph_meta.json")
        meta: dict[str, object] = {}
        if meta_file.is_file():
            raw = json.loads(meta_file.read_text(encoding="utf-8"))
            meta = raw if isinstance(raw, dict) else {}
        version = meta.get("format_version", FORMAT_VERSION)
        if version != FORMAT_VERSION:
            raise GraphError(f"format_version {version}, expected {FORMAT_VERSION}")
        with np.load(path, allow_pickle=False) as data:
            arrays = {name: data[name] for name in _ARRAYS if name in data.files}
        graph = WalkGraph(arrays, meta)
        log.info("walk graph: %d nodes, %d edges from %s", len(graph.node_lat),
                 len(graph._eu), path.name)
    except (OSError, ValueError, KeyError, TypeError, EOFError, zipfile.BadZipFile) as exc:
        log.warning("walk graph %s not usable (%s): straight segments", path.name, exc)
        graph = None
    _CACHE[str(path)] = (mtime, graph)
    return graph
