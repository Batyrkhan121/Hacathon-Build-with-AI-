"""Mini-route for up to 3 stops. Owner: CORE lane (task T23).

Approximate on purpose (no routing engine): nearest-neighbour order from the origin,
straight segments, walking time via app.geo.walk_minutes. Be honest in MiniRoute.note.
"""

from __future__ import annotations

from app.schemas import Candidate, GeoPoint, MiniRoute


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
    raise NotImplementedError("T23 (CORE)")
