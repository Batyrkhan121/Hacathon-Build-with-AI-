"""Geo helpers. Owner: CORE lane (task T20). Keep signatures; implement bodies + tests.

Privacy: the server only ever sees an H3 cell or a landmark id. `resolve_origin` turns
that into the cell centre / landmark point. Raw user coordinates never reach this code.
"""

from __future__ import annotations

from app.schemas import GeoPoint, Landmark, LocationInput, QueryIntent

WALK_SPEED_M_PER_MIN = 75.0  # 4.5 km/h
DETOUR_FACTOR = 1.3  # straight line -> real streets
DEFAULT_H3_RES = 9  # ~0.1 km² cell, edge ~200 m
MIN_RADIUS_M = 400
MAX_RADIUS_M = 2500
DEFAULT_RADIUS_M = 1200
ONE_WAY_SHARE = 0.35  # share of the time budget we allow for walking to the first stop


def haversine_m(a: GeoPoint, b: GeoPoint) -> float:
    """Great-circle distance in metres."""
    raise NotImplementedError("T20 (CORE)")


def walk_minutes(distance_m: float) -> int:
    """Walking time for a straight-line distance, with DETOUR_FACTOR; at least 1 minute."""
    raise NotImplementedError("T20 (CORE)")


def to_h3(point: GeoPoint, res: int = DEFAULT_H3_RES) -> str:
    raise NotImplementedError("T20 (CORE)")


def h3_center(cell: str) -> GeoPoint:
    raise NotImplementedError("T20 (CORE)")


def h3_resolution(cell: str) -> int:
    raise NotImplementedError("T20 (CORE)")


def match_landmark(text: str, landmarks: dict[str, Landmark]) -> Landmark | None:
    """Find a landmark mentioned in free text via aliases (case-insensitive, ru/kk/en)."""
    raise NotImplementedError("T20 (CORE)")


def resolve_origin(
    loc: LocationInput, landmarks: dict[str, Landmark]
) -> tuple[GeoPoint, str, str | None, str]:
    """Return (origin, h3_cell, landmark_id, area_label).

    h3_cell input -> origin = cell centre, area_label like "ваш район (≈0,1 км²)".
    landmark_id input -> origin = landmark point, h3_cell = to_h3(point), label "у <name>".
    Unknown landmark id -> raise KeyError (API maps it to HTTP 422).
    """
    raise NotImplementedError("T20 (CORE)")


def radius_for(intent: QueryIntent) -> int:
    """Search radius in metres from max_walk_min or ONE_WAY_SHARE * time_budget_min.

    No time info -> DEFAULT_RADIUS_M. Always clamp to [MIN_RADIUS_M, MAX_RADIUS_M].
    """
    raise NotImplementedError("T20 (CORE)")
