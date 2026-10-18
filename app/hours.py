"""Opening-hours evaluation. Owner: CORE lane (task T21); wave 3: FEATURES lane (T73).

Parse the common subset of OSM `opening_hours` deterministically, e.g.
  "24/7", "Mo-Su 10:00-22:00", "Mo-Fr 09:00-18:00; Sa 10:00-16:00", "Mo-Su 10:00-02:00",
  "Tu-Su 10:00-20:00; Mo off", "Mo-Fr 08:00-12:00,13:00-17:00", "sunrise-sunset" (-> low).
Anything not understood -> status "unknown", confidence "low" (never guess "open").
Notes are short human text; the UI shows them next to the hours badge. Russian by default,
`lang="kk" | "en"` gives the same note in Kazakh / English (T73). `unknown_note(lang)` is the
"no data, check before visiting" text that cards show for unknown hours.

Semantics follow the OSM spec: rules are separated by ";" and a later rule replaces earlier
ones for its days; ", " before a weekday starts an additional rule that adds to them; days
not covered by any rule are closed (today not described -> closed/medium). PH/SH rules are
ignored (we assume today is not a holiday) and the note warns about it.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from functools import lru_cache

from app.schemas import HoursVerdict, Lang

ASTANA_TZ = timezone(timedelta(hours=5))  # Kazakhstan is UTC+5 since 2024

DAY_MIN = 24 * 60
_DAYS = ("Mo", "Tu", "We", "Th", "Fr", "Sa", "Su")
_DAYS_RU = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")
_HOLIDAYS = ("PH", "SH")
_TOKEN = r"(?:Mo|Tu|We|Th|Fr|Sa|Su|PH|SH)"
_SELECTOR = re.compile(rf"^(?P<sel>{_TOKEN}(?:\s*[-,]\s*{_TOKEN})*)(?:\s+(?P<spec>.*))?$", re.I)
_ADDITIONAL = re.compile(rf"(?:(?<=\d)|(?<=off)|(?<=closed))\s*,\s*(?={_TOKEN}\b)", re.I)
_TIME_RANGE = re.compile(r"^(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})$")

NOTE_NO_DATA = "нет данных о часах работы"
NOTE_UNPARSED = "часы работы в OSM в нестандартном формате, проверьте перед визитом"
NOTE_ONLY_HOLIDAYS = "есть только праздничные часы, проверьте перед визитом"
NOTE_HOLIDAYS = "в праздники часы могут отличаться"
NOTE_UNKNOWN_CHECK = "нет данных о часах, проверьте перед визитом"  # = validate.UNKNOWN_HOURS_NOTE

_T: dict[str, dict[str, object]] = {
    "ru": {
        "no_data": NOTE_NO_DATA, "unknown_check": NOTE_UNKNOWN_CHECK, "unparsed": NOTE_UNPARSED,
        "only_holidays": NOTE_ONLY_HOLIDAYS, "holidays": NOTE_HOLIDAYS,
        "osm": "по данным OSM: {s}", "daily": "ежедневно", "day_off": "выходной",
        "all_day": "круглосуточно", "closes_in": "закроется через {d}",
        "closed_today": "по данным OSM сегодня не работает", "hours": "часы: {s}",
        "today_off": "сегодня выходной", "closed": "закрыто",
        "opens_in": "{head}, откроется через {d}", "opens_at": "{head}, откроется в {t}",
        "opens_tomorrow": "{head}, откроется завтра в {t}",
        "min": "{m} мин", "h": "{h} ч", "h_min": "{h} ч {m} мин", "days": _DAYS_RU,
    },
    "kk": {
        "no_data": "жұмыс уақыты туралы дерек жоқ",
        "unknown_check": "жұмыс уақыты туралы дерек жоқ, барар алдында тексеріңіз",
        "unparsed": "OSM-дегі жұмыс уақыты стандартты емес форматта, барар алдында тексеріңіз",
        "only_holidays": "тек мереке күнгі уақыт бар, барар алдында тексеріңіз",
        "holidays": "мереке күндері уақыт өзгеше болуы мүмкін",
        "osm": "OSM деректері бойынша: {s}", "daily": "күн сайын", "day_off": "демалыс",
        "all_day": "тәулік бойы", "closes_in": "жабылуына {d} қалды",
        "closed_today": "OSM деректері бойынша бүгін жұмыс істемейді", "hours": "уақыты: {s}",
        "today_off": "бүгін демалыс күні", "closed": "жабық",
        "opens_in": "{head}, ашылуына {d} қалды", "opens_at": "{head}, сағат {t}-де ашылады",
        "opens_tomorrow": "{head}, ертең сағат {t}-де ашылады",
        "min": "{m} мин", "h": "{h} сағ", "h_min": "{h} сағ {m} мин",
        "days": ("Дс", "Сс", "Ср", "Бс", "Жм", "Сб", "Жс"),
    },
    "en": {
        "no_data": "no opening-hours data",
        "unknown_check": "no opening-hours data, check before you go",
        "unparsed": "opening hours in OSM use a non-standard format, check before you go",
        "only_holidays": "only holiday hours are listed, check before you go",
        "holidays": "hours may differ on public holidays",
        "osm": "per OSM: {s}", "daily": "daily", "day_off": "closed", "all_day": "24/7",
        "closes_in": "closes in {d}", "closed_today": "per OSM: closed today",
        "hours": "hours: {s}", "today_off": "closed today", "closed": "closed",
        "opens_in": "{head}, opens in {d}", "opens_at": "{head}, opens at {t}",
        "opens_tomorrow": "{head}, opens tomorrow at {t}",
        "min": "{m} min", "h": "{h} h", "h_min": "{h} h {m} min", "days": _DAYS,
    },
}


def _tx(lang: str) -> dict[str, object]:
    return _T.get(lang, _T["ru"])


_CHECK = {"ru": "проверьте часы работы перед визитом",
          "kk": "жұмыс уақытын барар алдында тексеріңіз",
          "en": "check opening hours before you go"}


def check_note(lang: Lang = "ru") -> str:
    """Neutral note without any status claim (used when a note cannot be re-worded)."""
    return _CHECK.get(lang, _CHECK["ru"])


def unknown_note(lang: Lang = "ru") -> str:
    """What a card says when the hours are unknown: no data + check before visiting."""
    return str(_tx(lang)["unknown_check"])

Interval = tuple[int, int]  # minutes from the day's midnight; end may exceed DAY_MIN (overnight)
Week = tuple[tuple[Interval, ...] | None, ...]  # 7 days; None = day not described by any rule


class _Unparsable(ValueError):
    pass


def now_astana() -> datetime:
    return datetime.now(ASTANA_TZ)


# ------------------------------------------------------------------- parsing


def _day_index(token: str) -> int:
    return _DAYS.index(token.strip().capitalize())


def _parse_selector(selector: str) -> tuple[set[int], bool]:
    """Days of the week (0 = Mo) and whether PH/SH was mentioned."""
    days: set[int] = set()
    holidays = False
    for item in re.split(r"\s*,\s*", selector.strip()):
        if item.upper() in _HOLIDAYS:
            holidays = True
            continue
        if "-" in item:
            first, last = (part.strip() for part in item.split("-"))
            if first.upper() in _HOLIDAYS or last.upper() in _HOLIDAYS:
                raise _Unparsable(item)
            day, end = _day_index(first), _day_index(last)
            days.add(day)
            while day != end:  # wraps around Sunday: "Fr-Mo"
                day = (day + 1) % 7
                days.add(day)
        else:
            days.add(_day_index(item))
    return days, holidays


def _parse_times(spec: str) -> list[Interval]:
    spec = spec.strip()
    if spec.lower() in ("off", "closed"):
        return []
    if spec == "24/7":
        return [(0, DAY_MIN)]
    intervals: list[Interval] = []
    for part in spec.split(","):
        found = _TIME_RANGE.match(part.strip())
        if found is None:
            raise _Unparsable(part)
        sh, sm, eh, em = (int(x) for x in found.groups())
        if sh > 23 or sm > 59 or em > 59 or eh > 24 or (eh == 24 and em > 0):
            raise _Unparsable(part)
        start, end = sh * 60 + sm, eh * 60 + em
        if start == end:
            raise _Unparsable(part)
        intervals.append((start, end if end > start else end + DAY_MIN))
    return intervals


@lru_cache(maxsize=8192)
def _parse(raw: str) -> tuple[Week, bool]:
    """Week table + whether PH/SH rules were present. Raises _Unparsable. Cached: immutable."""
    week: list[list[Interval] | None] = [None] * 7
    holidays = False
    for rule in raw.split(";"):
        rule = " ".join(rule.split())
        if not rule:
            continue
        for n, piece in enumerate(_ADDITIONAL.split(rule)):
            found = _SELECTOR.match(piece.strip())
            if found is None:
                days, spec = set(range(7)), piece
            else:
                if found.group("spec") is None:
                    raise _Unparsable(piece)
                days, has_holiday = _parse_selector(found.group("sel"))
                holidays = holidays or has_holiday
                spec = found.group("spec")
                if not days:  # holiday-only rule, e.g. "PH off"
                    continue
            intervals = _parse_times(spec)
            for day in days:
                additional = n > 0 and intervals and week[day] is not None
                week[day] = [*(week[day] or []), *intervals] if additional else list(intervals)
    return tuple(None if day is None else tuple(day) for day in week), holidays


# ----------------------------------------------------------------- rendering


def _clock(minute: int) -> str:
    if minute == DAY_MIN:
        return "24:00"
    return f"{(minute // 60) % 24:02d}:{minute % 60:02d}"


def _duration(minutes: int, lang: str = "ru") -> str:
    t = _tx(lang)
    if minutes < 60:
        return str(t["min"]).format(m=minutes)
    hours, rest = divmod(minutes, 60)
    return str(t["h_min"]).format(h=hours, m=rest) if rest else str(t["h"]).format(h=hours)


def _days_label(days: list[int], lang: str = "ru") -> str:
    names = _tx(lang)["days"]
    if len(days) == 7:
        return str(_tx(lang)["daily"])
    runs: list[list[int]] = []
    for day in days:
        if runs and runs[-1][-1] == day - 1:
            runs[-1].append(day)
        else:
            runs.append([day])
    parts = [
        names[r[0]] if len(r) == 1 else f"{names[r[0]]}–{names[r[-1]]}" for r in runs
    ]
    return ", ".join(parts)


def _render(week: Week, lang: str = "ru") -> str:
    t = _tx(lang)
    groups: dict[tuple[Interval, ...], list[int]] = {}
    for day, day_intervals in enumerate(week):
        if day_intervals is not None:
            groups.setdefault(day_intervals, []).append(day)
    parts = []
    for intervals, days in groups.items():
        if not intervals:
            times = str(t["day_off"])
        elif intervals == ((0, DAY_MIN),):
            times = str(t["all_day"])
        else:
            times = ", ".join(f"{_clock(s)}–{_clock(e)}" for s, e in intervals)
        parts.append(f"{_days_label(days, lang)} {times}")
    return "; ".join(parts)


# ---------------------------------------------------------------- evaluation


def _spans(week: Week, weekday: int) -> list[Interval]:
    """Merged open spans from yesterday to tomorrow, in minutes from today's midnight."""
    raw: list[Interval] = []
    for offset in (-1, 0, 1):
        for start, end in week[(weekday + offset) % 7] or ():
            raw.append((offset * DAY_MIN + start, offset * DAY_MIN + end))
    merged: list[Interval] = []
    for start, end in sorted(raw):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _join(*parts: str) -> str:
    return " · ".join(p for p in parts if p)


def evaluate_hours(raw: str | None, at: datetime, lang: Lang = "ru") -> HoursVerdict:
    """Status of a place at local time `at` (tz-aware).

    - raw is None/empty -> unknown/low, note "нет данных о часах работы".
    - parsed and open -> open/high (medium if the rule looks partial, e.g. only some days known),
      fill closes_in_min.
    - parsed and closed -> closed/high, fill opens_in_min when it opens later today.
    - ranges past midnight ("18:00-02:00") must work, incl. early-morning `at`.
    `lang` only changes the wording of `note`; status, confidence and minutes are the same.
    """
    t = _tx(lang)
    if raw is None or not raw.strip():
        return HoursVerdict(status="unknown", confidence="low", note=str(t["no_data"]))
    try:
        week, holidays = _parse(raw)
    except (_Unparsable, ValueError):
        return HoursVerdict(status="unknown", confidence="low", note=str(t["unparsed"]))
    if all(day is None for day in week):
        return HoursVerdict(status="unknown", confidence="low", note=str(t["only_holidays"]))

    local = at.astimezone(ASTANA_TZ) if at.tzinfo else at.replace(tzinfo=ASTANA_TZ)
    weekday = local.weekday()
    now = local.hour * 60 + local.minute
    schedule = str(t["osm"]).format(s=_render(week, lang))
    holiday_note = str(t["holidays"]) if holidays else ""
    spans = _spans(week, weekday)

    current = next(((s, e) for s, e in spans if s <= now < e), None)
    if current is not None:
        closes_in = current[1] - now if current[1] < 2 * DAY_MIN else None
        closing = (str(t["closes_in"]).format(d=_duration(closes_in, lang))
                   if closes_in and closes_in <= 60 else "")
        return HoursVerdict(
            status="open",
            confidence="high",
            note=_join(closing, schedule, holiday_note),
            closes_in_min=closes_in,
        )

    if week[weekday] is None:
        return HoursVerdict(
            status="closed",
            confidence="medium",
            note=_join(str(t["closed_today"]), str(t["hours"]).format(s=_render(week, lang)),
                       holiday_note),
        )

    next_start = next((s for s, _ in spans if s > now), None)
    opens_in: int | None = None
    head = str(t["today_off"] if week[weekday] == () else t["closed"])
    if next_start is not None and next_start < DAY_MIN:
        opens_in = next_start - now
        if opens_in <= 60:
            head = str(t["opens_in"]).format(head=head, d=_duration(opens_in, lang))
        else:
            head = str(t["opens_at"]).format(head=head, t=_clock(next_start))
    elif next_start is not None and next_start < 2 * DAY_MIN:
        head = str(t["opens_tomorrow"]).format(head=head, t=_clock(next_start - DAY_MIN))
    return HoursVerdict(
        status="closed",
        confidence="high",
        note=_join(head, schedule, holiday_note),
        opens_in_min=opens_in,
    )
