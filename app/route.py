"""Mini-route for up to 3 stops. Owner: CORE lane (task T23).

Approximate on purpose (no routing engine): nearest-neighbour order from the origin,
straight segments, walking time via app.geo.walk_minutes. Be honest in MiniRoute.note.
"""

from __future__ import annotations

from app.geo import haversine_m, walk_minutes
from app.schemas import Candidate, GeoPoint, MiniRoute, RouteLeg

DEFAULT_VISIT_MIN = 20  # when the card has no estimate for a stop
MAX_ROUTE_STOPS = 3  # MiniRoute.stop_ids allows at most 3
ROUTE_NOTE = (
    "Маршрут приблизительный: прямые отрезки × 1,3, скорость 4,5 км/ч, без обратной дороги"
)


def _legs(origin: GeoPoint, ordered: list[Candidate]) -> list[RouteLeg]:
    legs: list[RouteLeg] = []
    here, here_id = origin, "origin"
    for cand in ordered:
        distance = haversine_m(here, cand.poi.geo)
        legs.append(
            RouteLeg(
                from_id=here_id,
                to_id=cand.poi.id,
                distance_m=round(distance),
                walk_min=walk_minutes(distance),
            )
        )
        here, here_id = cand.poi.geo, cand.poi.id
    return legs


def _nearest_neighbour(origin: GeoPoint, stops: list[Candidate]) -> list[Candidate]:
    pending = list(stops)
    ordered: list[Candidate] = []
    here = origin
    while pending:
        # ties keep the card order (rank)
        nearest = min(range(len(pending)), key=lambda i: (haversine_m(here, pending[i].poi.geo), i))
        ordered.append(pending.pop(nearest))
        here = ordered[-1].poi.geo
    return ordered


def plan_route(
    origin: GeoPoint,
    stops: list[Candidate],
    budget_min: int | None,
    visit_min: dict[str, int],
    max_stops: int = 3,
) -> MiniRoute | None:
    """Order stops, compute legs and totals; drop trailing stops that break the budget.

    visit_min maps poi_id -> planned minutes on site (use Card.visit_min_low).
    Return None when fewer than 1 stop remains.
    """
    unique: list[Candidate] = []
    for cand in stops:
        if all(cand.poi.id != u.poi.id for u in unique):
            unique.append(cand)
    kept = unique[: max(0, min(max_stops, MAX_ROUTE_STOPS))]  # stops come ranked best first
    if not kept:
        return None

    while True:
        ordered = _nearest_neighbour(origin, kept)
        legs = _legs(origin, ordered)
        visits = sum(visit_min.get(c.poi.id, DEFAULT_VISIT_MIN) for c in ordered)
        total = sum(leg.walk_min for leg in legs) + visits
        if budget_min is None or total <= budget_min or len(kept) == 1:
            break
        kept = kept[:-1]  # over budget: drop the lowest-ranked card, then re-order

    return MiniRoute(
        stop_ids=[c.poi.id for c in ordered],
        legs=legs,
        total_walk_m=sum(leg.distance_m for leg in legs),
        total_min=total,
        fits_budget=budget_min is None or total <= budget_min,
        path=[origin, *(c.poi.geo for c in ordered)],
        note=ROUTE_NOTE,
    )
