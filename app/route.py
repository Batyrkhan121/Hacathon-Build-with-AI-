"""Mini-route for up to 3 stops. Owner: CORE lane (T23); wave 3: FEATURES lane (T71).

Approximate on purpose (no routing engine): straight segments, walking time via
app.geo.walk_minutes. Be honest in MiniRoute.note.

Wave 3 (T71):
- the visiting order is the exact best one (all permutations of <= 3 stops, ties keep the
  card order) instead of nearest-neighbour;
- `loop=True` ("прогулка на N минут") adds the walk back to the start: a last leg to
  "origin", counted in total_min, and the path closes on the origin;
- `plan_walk()` picks up to 3 candidates that fit a time budget together (best total score,
  at most 2 of one category, no two places with the same name), before the cards are written;
- `route_note()` gives the honest note in ru / kk / en, with "allow extra time" for kids and
  wheelchairs (the pace itself is not slowed down: card and route times stay comparable).

Wave 5 (T91): with a walk graph (app.walkgraph) the legs, path, metres and minutes follow the
OSM footways and crossings (path_source="graph", crossings, steps_count); the stop order and
the budget trimming stay as above, but the budget is checked with the graph minutes. One route
gets walkgraph.ROUTE_LIMIT_S; any failure (no binding, no path, over the limit) -> the straight
route exactly as before (path_source="straight").
"""

from __future__ import annotations

import logging
import time
from collections import Counter
from collections.abc import Set as AbstractSet
from itertools import combinations, permutations

from app import walkgraph
from app.geo import haversine_m, walk_minutes
from app.schemas import Candidate, GeoPoint, Lang, MiniRoute, RouteCrossing, RouteLeg

log = logging.getLogger(__name__)

DEFAULT_VISIT_MIN = 20  # when the card has no estimate for a stop
MAX_ROUTE_STOPS = 3  # MiniRoute.stop_ids allows at most 3
WALK_POOL = 8  # plan_walk looks at the best N candidates
MAX_SAME_CATEGORY = 2
# Not a stop for a walk unless asked for explicitly (S1: a walk is not a fast-food crawl, and
# a sit-down restaurant does not fit a 30–60 minute walk either).
NOT_WALK_STOPS = frozenset({"fast_food", "bar", "nightlife", "restaurant"})
AT_ORIGIN_M = 100  # closer than this = where the person already is (P1: the mall, its café)
# A walk should have at least one place to walk to (P1: NU Park, not «mall + coffee»).
WALK_STOP_CATEGORIES = frozenset({"park", "viewpoint", "monument", "attraction", "religious"})
WALK_STOP_TAGS = frozenset({"walk", "outdoor", "view", "water"})
# Short stops on a walk (the walk itself is the activity); cards may later say more.
WALK_VISIT_MIN: dict[str, int] = {
    "park": 20, "viewpoint": 10, "monument": 10, "attraction": 15, "religious": 15,
    "museum": 40, "gallery": 30, "theatre": 15, "cinema": 15, "library": 15, "cafe": 20,
    "restaurant": 40, "fast_food": 15, "bar": 30, "nightlife": 30, "mall": 30,
    "playground": 20, "sport": 30, "entertainment": 30, "other": 15,
}
_NOTE: dict[str, dict[str, str]] = {
    "ru": {
        "base": "Маршрут приблизительный: прямые отрезки × 1,3, скорость 4,5 км/ч, {way}.",
        "graph": "Маршрут по тротуарам и переходам OSM, скорость 4,5 км/ч, {way}; "
                 "данные OSM бывают неполными.",
        "graph_wheelchair": "На коляске закладывайте больше времени; лестницы маршрут обходит, "
                            "бордюры не проверены.",
        "loop": "с возвращением к старту", "oneway": "без обратной дороги",
        "partly_straight": "Часть отрезков посчитана по прямой × 1,3.",
        "kids": "С ребёнком закладывайте больше времени.",
        "wheelchair": "На коляске закладывайте больше времени; маршрут не проверен на ступени "
                      "и бордюры.",
    },
    "kk": {
        "base": "Маршрут шамамен: түзу кесінділер × 1,3, жылдамдығы 4,5 км/сағ, {way}.",
        "graph": "Маршрут OSM жаяу жолдары мен өткелдері бойынша, жылдамдығы 4,5 км/сағ, "
                 "{way}; OSM деректері толық болмауы мүмкін.",
        "graph_wheelchair": "Арбамен көбірек уақыт қалдырыңыз; маршрут баспалдақтарды айналып "
                            "өтеді, жиектастар тексерілмеген.",
        "loop": "бастапқы нүктеге оралумен", "oneway": "кері жолсыз",
        "partly_straight": "Кейбір кесінділер түзу сызықпен × 1,3 есептелген.",
        "kids": "Баламен көбірек уақыт қалдырыңыз.",
        "wheelchair": "Арбамен көбірек уақыт қалдырыңыз; маршрут баспалдақ пен жиектасқа "
                      "тексерілмеген.",
    },
    "en": {
        "base": "Approximate route: straight segments × 1.3, 4.5 km/h, {way}.",
        "graph": "Route along OSM footways and crossings, 4.5 km/h, {way}; OSM data can be "
                 "incomplete.",
        "graph_wheelchair": "In a wheelchair, allow extra time; the route avoids stairs, kerbs "
                            "are not checked.",
        "loop": "back to the start", "oneway": "one way",
        "partly_straight": "Some segments are straight lines × 1.3.",
        "kids": "With kids, allow extra time.",
        "wheelchair": "In a wheelchair, allow extra time; the route is not checked for steps "
                      "or kerbs.",
    },
}
ROUTE_NOTE = _NOTE["ru"]["base"].format(way=_NOTE["ru"]["oneway"])


def route_note(lang: Lang = "ru", *, loop: bool = False, kids: bool = False,
               wheelchair: bool = False, graph: bool = False) -> str:
    texts = _NOTE.get(lang, _NOTE["ru"])
    way = texts["loop" if loop else "oneway"]
    parts = [texts["graph" if graph else "base"].format(way=way)]
    if wheelchair:
        parts.append(texts["graph_wheelchair" if graph else "wheelchair"])
    elif kids:
        parts.append(texts["kids"])
    return " ".join(parts)


def _legs(origin: GeoPoint, ordered: list[Candidate], loop: bool) -> list[RouteLeg]:
    legs: list[RouteLeg] = []
    here, here_id = origin, "origin"
    points = [(c.poi.geo, c.poi.id) for c in ordered]
    if loop and ordered:
        points.append((origin, "origin"))
    for geo, poi_id in points:
        distance = haversine_m(here, geo)
        legs.append(RouteLeg(from_id=here_id, to_id=poi_id, distance_m=round(distance),
                             walk_min=walk_minutes(distance)))
        here, here_id = geo, poi_id
    return legs


def _path_metres(origin: GeoPoint, ordered: list[Candidate], loop: bool) -> float:
    total, here = 0.0, origin
    for cand in ordered:
        total += haversine_m(here, cand.poi.geo)
        here = cand.poi.geo
    return total + (haversine_m(here, origin) if loop and ordered else 0.0)


def _best_order(origin: GeoPoint, stops: list[Candidate], loop: bool) -> list[Candidate]:
    """Shortest visiting order of <= 3 stops; ties keep the card order (rank)."""
    best = min(
        permutations(range(len(stops))),
        key=lambda p: (round(_path_metres(origin, [stops[i] for i in p], loop)), p),
    )
    return [stops[i] for i in best]


def _total_min(legs: list[RouteLeg], ordered: list[Candidate], visit_min: dict[str, int]) -> int:
    visits = sum(visit_min.get(c.poi.id, DEFAULT_VISIT_MIN) for c in ordered)
    return sum(leg.walk_min for leg in legs) + visits


MAX_CROSSINGS = 50  # MiniRoute.crossings max_length


def _graph_legs(
    graph: walkgraph.WalkGraph,
    origin: GeoPoint,
    ordered: list[Candidate],
    loop: bool,
    mode: walkgraph.Mode,
    origin_landmark: str | None,
    deadline: float,
) -> list[walkgraph.GraphPath] | None:
    """Graph legs origin -> stops (-> origin); None on any failure or past the deadline."""
    remaining = deadline - time.perf_counter()
    if remaining <= 0 or not ordered:
        return None
    points = [origin, *(c.poi.geo for c in ordered)]
    pois: list[str | None] = [None, *(c.poi.id for c in ordered)]
    landmarks: list[str | None] = [origin_landmark, *(None for _ in ordered)]
    if loop:
        points.append(origin)
        pois.append(None)
        landmarks.append(origin_landmark)
    return graph.route_through(points, mode, pois=pois, landmarks=landmarks, limit_s=remaining)


def _graph_total(legs: list[walkgraph.GraphPath], ordered: list[Candidate],
                 visit_min: dict[str, int]) -> int:
    return sum(leg.walk_min for leg in legs) + sum(
        visit_min.get(c.poi.id, DEFAULT_VISIT_MIN) for c in ordered
    )


def _graph_route(
    origin: GeoPoint,
    ordered: list[Candidate],
    legs: list[walkgraph.GraphPath],
    loop: bool,
    total: int,
    budget_min: int | None,
    note: str,
    partly_note: str,
) -> MiniRoute:
    ids = [c.poi.id for c in ordered] + (["origin"] if loop else [])
    route_legs: list[RouteLeg] = []
    from_id = "origin"
    for leg, to_id in zip(legs, ids, strict=True):
        route_legs.append(RouteLeg(from_id=from_id, to_id=to_id, distance_m=round(leg.length_m),
                                   walk_min=leg.walk_min))
        from_id = to_id
    path: list[GeoPoint] = []
    for leg in legs:
        points = leg.points
        if path and (path[-1].lat, path[-1].lon) == points[0]:
            points = points[1:]
        path.extend(GeoPoint(lat=lat, lon=lon) for lat, lon in points)
    crossings: list[RouteCrossing] = []
    seen: set[tuple[float, float]] = set()
    for leg in legs:
        for c in leg.crossings:
            if (c.lat, c.lon) not in seen and len(crossings) < MAX_CROSSINGS:
                seen.add((c.lat, c.lon))
                crossings.append(RouteCrossing(lat=c.lat, lon=c.lon, kind=c.kind))  # type: ignore[arg-type]
    return MiniRoute(
        stop_ids=[c.poi.id for c in ordered],
        legs=route_legs,
        total_walk_m=sum(leg.distance_m for leg in route_legs),
        total_min=total,
        fits_budget=budget_min is None or total <= budget_min,
        path=path,
        note=f"{note} {partly_note}" if any(leg.straight for leg in legs) else note,
        path_source="graph",
        crossings=crossings,
        steps_count=sum(leg.steps for leg in legs),
    )


def plan_route(
    origin: GeoPoint,
    stops: list[Candidate],
    budget_min: int | None,
    visit_min: dict[str, int],
    max_stops: int = 3,
    *,
    loop: bool = False,
    note: str | None = None,
    graph: walkgraph.WalkGraph | None = None,
    mode: walkgraph.Mode = walkgraph.WALK,
    origin_landmark: str | None = None,
    graph_note: str | None = None,
    lang: Lang = "ru",
) -> MiniRoute | None:
    """Order stops, compute legs and totals; drop trailing stops that break the budget.

    visit_min maps poi_id -> planned minutes on site (use Card.visit_min_low).
    Return None when fewer than 1 stop remains. `loop` adds the walk back to the start.
    With `graph`: legs, path and minutes along the walk graph (path_source="graph"); legs
    that fell back to straight segments add a sentence in `lang` to the note; any failure or
    no leg on the graph -> exactly the straight route.
    """
    if graph is not None:
        texts = _NOTE.get(lang, _NOTE["ru"])
        routed = _plan_on_graph(origin, stops, budget_min, visit_min, max_stops, loop=loop,
                                graph=graph, mode=mode, origin_landmark=origin_landmark,
                                note=graph_note or route_note(lang, loop=loop, graph=True),
                                partly_note=texts["partly_straight"])
        if routed is not None:
            return routed
    unique: list[Candidate] = []
    for cand in stops:
        if all(cand.poi.id != u.poi.id for u in unique):
            unique.append(cand)
    kept = unique[: max(0, min(max_stops, MAX_ROUTE_STOPS))]  # stops come ranked best first
    if not kept:
        return None

    while True:
        ordered = _best_order(origin, kept, loop)
        legs = _legs(origin, ordered, loop)
        total = _total_min(legs, ordered, visit_min)
        if budget_min is None or total <= budget_min or len(kept) == 1:
            break
        kept = kept[:-1]  # over budget: drop the lowest-ranked card, then re-order

    path = [origin, *(c.poi.geo for c in ordered)]
    if loop:
        path.append(origin)
    return MiniRoute(
        stop_ids=[c.poi.id for c in ordered],
        legs=legs,
        total_walk_m=sum(leg.distance_m for leg in legs),
        total_min=total,
        fits_budget=budget_min is None or total <= budget_min,
        path=path,
        note=note or route_note("ru", loop=loop),
    )


def _plan_on_graph(
    origin: GeoPoint,
    stops: list[Candidate],
    budget_min: int | None,
    visit_min: dict[str, int],
    max_stops: int,
    *,
    loop: bool,
    graph: walkgraph.WalkGraph,
    mode: walkgraph.Mode,
    origin_landmark: str | None,
    note: str,
    partly_note: str,
) -> MiniRoute | None:
    """plan_route on the graph; None (-> straight route) on any failure, a bug included."""
    try:
        return _plan_on_graph_unsafe(origin, stops, budget_min, visit_min, max_stops, loop=loop,
                                     graph=graph, mode=mode, origin_landmark=origin_landmark,
                                     note=note, partly_note=partly_note)
    except Exception as exc:  # noqa: BLE001 - the straight route is always there
        log.error("walk graph route failed (%s): straight segments", type(exc).__name__)
        return None


def _plan_on_graph_unsafe(
    origin: GeoPoint,
    stops: list[Candidate],
    budget_min: int | None,
    visit_min: dict[str, int],
    max_stops: int,
    *,
    loop: bool,
    graph: walkgraph.WalkGraph,
    mode: walkgraph.Mode,
    origin_landmark: str | None,
    note: str,
    partly_note: str,
) -> MiniRoute | None:
    unique: list[Candidate] = []
    for cand in stops:
        if all(cand.poi.id != u.poi.id for u in unique):
            unique.append(cand)
    kept = unique[: max(0, min(max_stops, MAX_ROUTE_STOPS))]
    if not kept:
        return None
    deadline = time.perf_counter() + walkgraph.ROUTE_LIMIT_S
    while True:
        ordered = _best_order(origin, kept, loop)
        legs = _graph_legs(graph, origin, ordered, loop, mode, origin_landmark, deadline)
        if legs is None or all(leg.straight for leg in legs):
            return None  # no leg follows the graph: the plain straight route, with its note
        total = _graph_total(legs, ordered, visit_min)
        if budget_min is None or total <= budget_min or len(kept) == 1:
            return _graph_route(origin, ordered, legs, loop, total, budget_min, note,
                                partly_note)
        kept = kept[:-1]  # over budget by the graph: drop the lowest-ranked card


def _fit_walk_on_graph(
    origin: GeoPoint,
    picked: list[Candidate],
    budget_min: int,
    loop: bool,
    graph: walkgraph.WalkGraph,
    mode: walkgraph.Mode,
    origin_landmark: str | None,
) -> list[Candidate] | None:
    """The picked walk checked with graph minutes: drop the lowest-scored stop until it fits.
    None (-> keep the straight pick) when the graph fails, a bug included."""
    try:
        return _fit_walk_on_graph_unsafe(origin, picked, budget_min, loop, graph, mode,
                                         origin_landmark)
    except Exception as exc:  # noqa: BLE001 - the straight pick is always there
        log.error("walk graph check of the walk failed (%s): straight pick", type(exc).__name__)
        return None


def _fit_walk_on_graph_unsafe(
    origin: GeoPoint,
    picked: list[Candidate],
    budget_min: int,
    loop: bool,
    graph: walkgraph.WalkGraph,
    mode: walkgraph.Mode,
    origin_landmark: str | None,
) -> list[Candidate] | None:
    deadline = time.perf_counter() + walkgraph.ROUTE_LIMIT_S
    visits = {c.poi.id: walk_visit_minutes(c) for c in picked}
    current = list(picked)
    while current:
        ordered = _best_order(origin, current, loop)
        legs = _graph_legs(graph, origin, ordered, loop, mode, origin_landmark, deadline)
        if legs is None:
            return None
        if _graph_total(legs, ordered, visits) <= budget_min:
            return ordered
        weakest = min(current, key=lambda c: (c.score, -current.index(c)))
        current.remove(weakest)
    return []


def walk_visit_minutes(cand: Candidate) -> int:
    return WALK_VISIT_MIN.get(cand.poi.category, WALK_VISIT_MIN["other"])


def plan_walk(
    origin: GeoPoint,
    candidates: list[Candidate],
    budget_min: int,
    *,
    loop: bool = True,
    max_stops: int = MAX_ROUTE_STOPS,
    wanted_categories: AbstractSet[str] = frozenset(),
    graph: walkgraph.WalkGraph | None = None,
    mode: walkgraph.Mode = walkgraph.WALK,
    origin_landmark: str | None = None,
) -> list[Candidate]:
    """Up to `max_stops` candidates that fit `budget_min` together, in visiting order.

    Anchor: if the best-ranked eligible candidate fits alone, it must be in the walk; among
    such sets more stops win, then the higher total score, then the shorter walk (P1: NU Park
    is in the loop, the mall you stand at is not). Without a fitting anchor: a set with a walk
    stop (park, viewpoint, monument, sight, outdoor / waterside) beats one without, then the
    total score, then more stops, then the shorter walk.
    Never a stop: places closer than AT_ORIGIN_M (where the person already is), fast food /
    bars / nightlife unless in `wanted_categories`. At most MAX_SAME_CATEGORY of one category
    and no two places with the same name. [] when not even one stop fits.
    With `graph` (T91) the picked walk is checked with graph minutes: the lowest-scored stop
    is dropped until it fits; a graph failure keeps the straight pick.
    """
    pool = [c for c in candidates if walk_eligible(c, wanted_categories)][:WALK_POOL]
    visits = {c.poi.id: walk_visit_minutes(c) for c in pool}

    def fit(combo: tuple[Candidate, ...]) -> tuple[list[Candidate], int] | None:
        ordered = _best_order(origin, list(combo), loop)
        total = _total_min(_legs(origin, ordered, loop), ordered, visits)
        return (ordered, total) if total <= budget_min else None

    anchor = pool[0] if pool and fit((pool[0],)) is not None else None
    best_key: tuple[float, ...] | None = None
    best: list[Candidate] = []
    for size in range(1, min(max_stops, MAX_ROUTE_STOPS, len(pool)) + 1):
        for combo in combinations(pool, size):
            if (anchor is not None and anchor not in combo) or not _diverse(combo):
                continue
            fitted = fit(combo)
            if fitted is None:
                continue
            ordered, total = fitted
            score = round(sum(c.score for c in combo), 6)
            if anchor is not None:
                key: tuple[float, ...] = (size, score, -total)
            else:
                key = (any(is_walk_stop(c) for c in combo), score, size, -total)
            if best_key is None or key > best_key:
                best_key, best = key, ordered
    if graph is not None and best:  # T91: the budget is checked with the graph minutes
        on_graph = _fit_walk_on_graph(origin, best, budget_min, loop, graph, mode,
                                      origin_landmark)
        if on_graph is not None:
            return on_graph
    return best


def walk_eligible(cand: Candidate, wanted_categories: AbstractSet[str] = frozenset()
                  ) -> bool:
    """May be a stop (or an alternative card) of a walk: not where the person already is, not
    fast food / bar / nightlife / restaurant unless that category was asked for."""
    skip = NOT_WALK_STOPS - set(wanted_categories)
    return cand.poi.category not in skip and cand.distance_m >= AT_ORIGIN_M


def is_walk_stop(cand: Candidate) -> bool:
    return (cand.poi.category in WALK_STOP_CATEGORIES
            or bool(WALK_STOP_TAGS & set(cand.poi.tags)))


def _diverse(combo: tuple[Candidate, ...]) -> bool:
    categories = Counter(c.poi.category for c in combo)
    names = Counter(c.poi.name.strip().casefold() for c in combo)
    return max(categories.values()) <= MAX_SAME_CATEGORY and max(names.values()) == 1
