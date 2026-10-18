"""Opening-hours evaluation. Owner: CORE lane (task T21).

Parse the common subset of OSM `opening_hours` deterministically, e.g.
  "24/7", "Mo-Su 10:00-22:00", "Mo-Fr 09:00-18:00; Sa 10:00-16:00", "Mo-Su 10:00-02:00",
  "Tu-Su 10:00-20:00; Mo off", "Mo-Fr 08:00-12:00,13:00-17:00", "sunrise-sunset" (-> low).
Anything not understood -> status "unknown", confidence "low" (never guess "open").
Notes are short human text in Russian; the UI shows them next to the hours badge.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.schemas import HoursVerdict

ASTANA_TZ = timezone(timedelta(hours=5))  # Kazakhstan is UTC+5 since 2024


def now_astana() -> datetime:
    return datetime.now(ASTANA_TZ)


def evaluate_hours(raw: str | None, at: datetime) -> HoursVerdict:
    """Status of a place at local time `at` (tz-aware).

    - raw is None/empty -> unknown/low, note "нет данных о часах работы".
    - parsed and open -> open/high (medium if the rule looks partial, e.g. only some days known),
      fill closes_in_min.
    - parsed and closed -> closed/high, fill opens_in_min when it opens later today.
    - ranges past midnight ("18:00-02:00") must work, incl. early-morning `at`.
    """
    raise NotImplementedError("T21 (CORE)")
