"""Geo helpers. Owner: CORE lane (task T20). Keep signatures; implement bodies + tests.

Privacy: the server only ever sees an H3 cell or a landmark id. `resolve_origin` turns
that into the cell centre / landmark point. Raw user coordinates never reach this code.
"""

from __future__ import annotations

import math
import re
from decimal import ROUND_HALF_UP, Decimal
from functools import lru_cache

import h3

from app.schemas import MAX_H3_RES, GeoPoint, Landmark, Lang, LocationInput, QueryIntent

WALK_SPEED_M_PER_MIN = 75.0  # 4.5 km/h
DETOUR_FACTOR = 1.3  # straight line -> real streets
DEFAULT_H3_RES = 9  # ~0.1 km² cell, edge ~200 m
# Wave 6 (T101): a person's cell may be up to r10 (≈1,8 га in Astana, schemas.MAX_H3_RES);
# a landmark's cell stays r9 (labels, tests and the context of a landmark do not change).
LANDMARK_H3_RES = 9
MIN_RADIUS_M = 400
MAX_RADIUS_M = 2500
DEFAULT_RADIUS_M = 1200
ONE_WAY_SHARE = 0.35  # share of the time budget we allow for walking to the first stop

EARTH_RADIUS_M = 6_371_008.8
NEAR_LANDMARK_M = 700  # "ваш район у EXPO" only when a landmark is this close to the cell centre
# Russian / Kazakh case endings: "у Байтерека", "возле Хан-Шатыра". Only for longer Cyrillic
# aliases, so that "мега" never matches "мегаполис" and "экспо" never matches "экспонат".
_SUFFIX_MIN_ALIAS_LEN = 5
_CYR_SUFFIX = "[а-яәіңғүұқөһ]{0,2}"
_CYR_LETTER = re.compile("[а-яәіңғүұқөһ]")
_ALIAS_PARTS = re.compile(r"[\s\-]+")


def haversine_m(a: GeoPoint, b: GeoPoint) -> float:
    """Great-circle distance in metres."""
    lat1, lat2 = math.radians(a.lat), math.radians(b.lat)
    dlat = lat2 - lat1
    dlon = math.radians(b.lon - a.lon)
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(h)))


def walk_minutes(distance_m: float) -> int:
    """Walking time for a straight-line distance, with DETOUR_FACTOR; at least 1 minute."""
    minutes = max(distance_m, 0.0) * DETOUR_FACTOR / WALK_SPEED_M_PER_MIN
    return max(1, math.ceil(minutes - 1e-9))  # epsilon: 750 m is 13 min, not 14


def to_h3(point: GeoPoint, res: int = DEFAULT_H3_RES) -> str:
    return h3.latlng_to_cell(point.lat, point.lon, res)


def h3_center(cell: str) -> GeoPoint:
    lat, lon = h3.cell_to_latlng(cell)
    return GeoPoint(lat=lat, lon=lon)


def h3_resolution(cell: str) -> int:
    return h3.get_resolution(cell)


def _normalize(text: str) -> str:
    return text.lower().replace("ё", "е")


@lru_cache(maxsize=1024)
def _alias_pattern(alias: str) -> re.Pattern[str]:
    norm = _normalize(alias.strip())
    body = r"[\s\-]+".join(re.escape(part) for part in _ALIAS_PARTS.split(norm) if part)
    suffix = ""
    if len(norm) >= _SUFFIX_MIN_ALIAS_LEN and _CYR_LETTER.fullmatch(norm[-1]):
        suffix = _CYR_SUFFIX
    return re.compile(rf"(?<!\w){body}{suffix}(?!\w)")


def match_landmark(text: str, landmarks: dict[str, Landmark]) -> Landmark | None:
    """Find a landmark mentioned in free text via aliases (case-insensitive, ru/kk/en)."""
    norm = _normalize(text)
    if not norm.strip():
        return None
    best: tuple[int, int, str] | None = None  # (-alias length, position, id)
    for lm in landmarks.values():
        for alias in lm.aliases:
            if not alias.strip():
                continue
            found = _alias_pattern(alias).search(norm)
            if found is None:
                continue
            key = (-len(alias.strip()), found.start(), lm.id)
            if best is None or key < best:
                best = key
    return landmarks[best[2]] if best else None


def _display_name(lm: Landmark) -> str:
    return lm.names.get("ru") or next(iter(lm.names.values()), lm.id)


def _nearest_landmark(point: GeoPoint, landmarks: dict[str, Landmark]) -> Landmark | None:
    scored = [(haversine_m(point, lm.geo), lm.id) for lm in landmarks.values()]
    if not scored:
        return None
    distance, lm_id = min(scored)
    return landmarks[lm_id] if distance <= NEAR_LANDMARK_M else None


def resolve_origin(
    loc: LocationInput, landmarks: dict[str, Landmark]
) -> tuple[GeoPoint, str, str | None, str]:
    """Return (origin, h3_cell, landmark_id, area_label).

    h3_cell input -> origin = cell centre, area_label like "ваш район (≈13 га)" (r9) or
    "ваш район (≈1,8 га)" (r10). Cells finer than MAX_H3_RES (r10) are rejected with
    ValueError (privacy: no near-exact points), even if the schema check was skipped.
    landmark_id input -> origin = landmark point, h3_cell = its LANDMARK_H3_RES (r9) cell,
    label "у <name>". Unknown landmark id -> raise KeyError (API maps it to HTTP 422).
    """
    if loc.landmark_id is not None:
        lm = landmarks.get(loc.landmark_id)
        if lm is None:
            raise KeyError(f"unknown landmark id: {loc.landmark_id}")
        return lm.geo, to_h3(lm.geo, LANDMARK_H3_RES), lm.id, f"у {_display_name(lm)}"

    cell = loc.h3_cell
    if cell is None or not h3.is_valid_cell(cell):
        raise ValueError("invalid H3 cell")
    if h3.get_resolution(cell) > MAX_H3_RES:  # finer cells are effectively raw coordinates
        raise ValueError(f"H3 cell finer than r{MAX_H3_RES} is not accepted")
    origin = h3_center(cell)
    label = f"ваш район (≈{hectares_text(h3.cell_area(cell, unit='km^2'))} га)"
    near = _nearest_landmark(origin, landmarks)
    if near is not None:
        label += f" у {_display_name(near)}"
    return origin, cell, None, label


def hectares_text(km2: float, lang: Lang = "ru") -> str:
    """A cell area in hectares, the number only: «13» (r9), «1,8» (r10; «0,0 км²» would say
    nothing). Same as the browser (web/app.js cellAreaHa + Intl.NumberFormat with
    maximumFractionDigits): from 10 ha a whole number, below at most one decimal (no trailing
    zero), half up; decimal comma for ru / kk, point for en; digit groups like Intl
    (ru / kk from 10 000 with a no-break space, en from 1 000 with a comma)."""
    ha = Decimal(km2 * 100)  # the exact float value, as Intl rounds it
    step = Decimal(1) if ha >= 10 else Decimal("0.1")
    number = ha.quantize(step, rounding=ROUND_HALF_UP)
    whole, _, frac = f"{number:f}".partition(".")
    frac = frac.rstrip("0")
    en = lang == "en"
    if len(whole) >= (4 if en else 5):
        whole = f"{int(whole):,}".replace(",", "," if en else " ")
    return whole + (("." if en else ",") + frac if frac else "")


def radius_for(intent: QueryIntent) -> int:
    """Search radius in metres from max_walk_min or ONE_WAY_SHARE * time_budget_min.

    No time info -> DEFAULT_RADIUS_M. Always clamp to [MIN_RADIUS_M, MAX_RADIUS_M].
    """
    if intent.max_walk_min:
        minutes = float(intent.max_walk_min)
    elif intent.time_budget_min:
        minutes = ONE_WAY_SHARE * intent.time_budget_min
    else:
        return DEFAULT_RADIUS_M
    metres = round(minutes * WALK_SPEED_M_PER_MIN / DETOUR_FACTOR)
    return int(min(MAX_RADIUS_M, max(MIN_RADIUS_M, metres)))
