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
"""

from __future__ import annotations

from collections import Counter
from itertools import combinations, permutations

from app.geo import haversine_m, walk_minutes
from app.schemas import Candidate, GeoPoint, Lang, MiniRoute, RouteLeg

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
        "loop": "с возвращением к старту", "oneway": "без обратной дороги",
        "kids": "С ребёнком закладывайте больше времени.",
        "wheelchair": "На коляске закладывайте больше времени; маршрут не проверен на ступени "
                      "и бордюры.",
    },
    "kk": {
        "base": "Маршрут шамамен: түзу кесінділер × 1,3, жылдамдығы 4,5 км/сағ, {way}.",
        "loop": "бастапқы нүктеге оралумен", "oneway": "кері жолсыз",
        "kids": "Баламен көбірек уақыт қалдырыңыз.",
        "wheelchair": "Арбамен көбірек уақыт қалдырыңыз; маршрут баспалдақ пен жиектасқа "
                      "тексерілмеген.",
    },
    "en": {
        "base": "Approximate route: straight segments × 1.3, 4.5 km/h, {way}.",
        "loop": "back to the start", "oneway": "one way",
        "kids": "With kids, allow extra time.",
        "wheelchair": "In a wheelchair, allow extra time; the route is not checked for steps "
                      "or kerbs.",
    },
}
ROUTE_NOTE = _NOTE["ru"]["base"].format(way=_NOTE["ru"]["oneway"])


def route_note(lang: Lang = "ru", *, loop: bool = False, kids: bool = False,
               wheelchair: bool = False) -> str:
    texts = _NOTE.get(lang, _NOTE["ru"])
    parts = [texts["base"].format(way=texts["loop" if loop else "oneway"])]
    if wheelchair:
        parts.append(texts["wheelchair"])
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


def _hours_fit(origin: GeoPoint, ordered: list[Candidate], visit_min: dict[str, int]) -> bool:
    """Every visit must finish before the first known closure, including previous stops."""
    elapsed = 0
    here = origin
    for cand in ordered:
        elapsed += walk_minutes(haversine_m(here, cand.poi.geo))
        elapsed += visit_min.get(cand.poi.id, DEFAULT_VISIT_MIN)
        if cand.hours.status == "closed" and cand.hours.confidence in {"high", "medium"}:
            return False
        closes = cand.hours.closes_in_min
        if cand.hours.status == "open" and closes is not None and elapsed > closes:
            return False
        here = cand.poi.geo
    return True


def _best_order(origin: GeoPoint, stops: list[Candidate], loop: bool,
                visit_min: dict[str, int]) -> list[Candidate] | None:
    """Shortest order that respects closing times; ties keep the card order (rank)."""
    orders = (p for p in permutations(range(len(stops)))
              if _hours_fit(origin, [stops[i] for i in p], visit_min))
    best = min(orders,
               key=lambda p: (round(_path_metres(origin, [stops[i] for i in p], loop)), p),
               default=None)
    return [stops[i] for i in best] if best is not None else None


def _total_min(legs: list[RouteLeg], ordered: list[Candidate], visit_min: dict[str, int]) -> int:
    visits = sum(visit_min.get(c.poi.id, DEFAULT_VISIT_MIN) for c in ordered)
    return sum(leg.walk_min for leg in legs) + visits


def plan_route(
    origin: GeoPoint,
    stops: list[Candidate],
    budget_min: int | None,
    visit_min: dict[str, int],
    max_stops: int = 3,
    *,
    loop: bool = False,
    note: str | None = None,
) -> MiniRoute | None:
    """Order stops, compute legs and totals; drop trailing stops that break the budget.

    visit_min maps poi_id -> planned minutes on site (use Card.visit_min_low).
    Return None when fewer than 1 stop remains. `loop` adds the walk back to the start.
    """
    unique: list[Candidate] = []
    for cand in stops:
        if all(cand.poi.id != u.poi.id for u in unique):
            unique.append(cand)
    kept = unique[: max(0, min(max_stops, MAX_ROUTE_STOPS))]  # stops come ranked best first
    if not kept:
        return None

    while kept:
        ordered = _best_order(origin, kept, loop, visit_min)
        if ordered is None:
            kept = kept[:-1]
            continue
        legs = _legs(origin, ordered, loop)
        total = _total_min(legs, ordered, visit_min)
        if budget_min is None or total <= budget_min:
            break
        kept = kept[:-1]  # over budget: drop the lowest-ranked card, then re-order
    else:
        return None

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


def walk_visit_minutes(cand: Candidate) -> int:
    return WALK_VISIT_MIN.get(cand.poi.category, WALK_VISIT_MIN["other"])


def plan_walk(
    origin: GeoPoint,
    candidates: list[Candidate],
    budget_min: int,
    *,
    loop: bool = True,
    max_stops: int = MAX_ROUTE_STOPS,
    wanted_categories: frozenset[str] | set[str] = frozenset(),
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
    """
    pool = [c for c in candidates if walk_eligible(c, wanted_categories)][:WALK_POOL]
    visits = {c.poi.id: walk_visit_minutes(c) for c in pool}

    def fit(combo: tuple[Candidate, ...]) -> tuple[list[Candidate], int] | None:
        ordered = _best_order(origin, list(combo), loop, visits)
        if ordered is None:
            return None
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
    return best


def walk_eligible(cand: Candidate, wanted_categories: frozenset[str] | set[str] = frozenset()
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
