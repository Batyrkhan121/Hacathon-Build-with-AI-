"""Dated events at venues: data/events.json (DATA, T122) -> search candidates and event cards.

Owner: FEATURES lane (T123). Contract 1.3.0, format: docs/ARCHITECTURE.md, section 5.

- load_events(path): a tolerant reader. No file, an empty list, broken JSON or a record that does
  not fit the format -> those events are skipped quietly (a log line with counts only, never the
  content); the answer never fails because of events.
- current(events, now): the events that matter at the request time:
  * not over: now < ends_at; an event without ends_at is taken to last NO_END_MIN after its
    start, and never past the end of its start day (24:00 UTC+5) — an evening concert is gone
    at midnight, a morning one is not shown in the evening;
  * starting today (the local day of the request) or within AHEAD_H hours (late evening: the
    night's events after midnight) — a walk assistant, not a calendar for next month.
- is_live(event, now): «идёт сейчас» only for a session (at most SESSION_MAX long) with
  starts_at ≤ now < ends_at, both known. A longer event (a run: an exhibition 01.10–30.11) is
  never «on now» — its daily hours are not known — it «runs until 30.11 18:00». Otherwise the
  card says «начало в ЧЧ:ММ». A time on another day than today always carries its date.
- as_poi / verdict: an event enters the search as a POI (id = event id, sources ["manual"]) with
  an hours verdict unknown/low whose note is the event time written by the code.
Times are compared tz-aware; the text shows local time (UTC+5, app.hours.ASTANA_TZ).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.hours import ASTANA_TZ
from app.schemas import POI, TAG_VOCAB, Category, GeoPoint, HoursVerdict, Lang

log = logging.getLogger(__name__)

EVENT_PREFIX = "event:"
NO_END_MIN = 120  # an event without ends_at is taken to last this long (and not past 24:00)
AHEAD_H = 6  # besides today: events starting within this many hours (after midnight)
SESSION_MAX = timedelta(hours=12)  # longer = a run (exhibition): daily hours not known
DATA_CONFIDENCE = 0.8  # checked by a person against the organiser's page (pipeline.events)
# Never a source of events: maps whose terms forbid storing (CLAUDE.md) and ticket
# aggregators (T132). The same list as pipeline.events.FORBIDDEN_SOURCES (DATA, tested to
# cover it): the build refuses them, and this reader refuses them once more. pipeline/ is not
# in the server image, so the list is repeated here, not imported. A name with a dot matches
# that domain and its subdomains, a name without one matches a whole host label: afisha.
# yandex.kz is refused, notyandex.kz is not.
FORBIDDEN_SOURCES: frozenset[str] = frozenset({
    "2gis", "yandex", "ya.ru",
    "ticketon.kz", "sxodim.com", "kino.kz", "kassir.ru", "ticketland.ru", "afisha.ru",
    "radario.ru", "timepad.ru", "intickets.ru", "ticketmaster.com", "eventbrite.com",
})
SourceKind = Literal["venue", "organizer", "city_portal"]


class Event(BaseModel):
    """One record of data/events.json; fields we do not use (price, …) are ignored."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    id: str = Field(pattern=r"^event:[a-z0-9][a-z0-9-]{2,79}$")
    title: dict[str, str]  # ru, and kk / en when known
    category: Category
    tags: tuple[str, ...] = ()
    starts_at: AwareDatetime
    ends_at: AwareDatetime | None = None
    venue: str | None = None
    address: str | None = None
    geo: GeoPoint
    venue_poi_id: str | None = None
    source_url: str = Field(max_length=2048, pattern=r"^https://\S+$")
    source_name: str | None = None
    fetched_at: str | None = None
    indoor: bool | None = None
    kids_ok: bool | None = None
    source_kind: SourceKind | None = None  # T132: venue | organizer | city_portal (if given)

    @model_validator(mode="after")
    def _checked(self) -> Event:
        if not (self.title.get("ru") or "").strip():
            raise ValueError("title.ru is required")
        if self.ends_at is not None and self.ends_at <= self.starts_at:
            raise ValueError("ends_at must be after starts_at")
        if forbidden_source(urlsplit(self.source_url).hostname or ""):
            raise ValueError("source not allowed")
        return self

    def title_in(self, lang: Lang) -> str:
        return (self.title.get(lang) or self.title["ru"]).strip()

    def place_line(self) -> str | None:
        """Card.address of an event: the venue and its address («Астана Опера, ул. …»)."""
        parts = [p.strip() for p in (self.venue, self.address) if p and p.strip()]
        if len(parts) == 2 and parts[1].casefold() in parts[0].casefold():
            parts = parts[:1]
        return ", ".join(parts) or None


_CACHE: dict[str, tuple[tuple[int, int], list[Event]]] = {}


def forbidden_source(host: str) -> bool:
    """The host is a map or a ticket aggregator (FORBIDDEN_SOURCES), by host labels."""
    host = host.lower().rstrip(".")
    labels = host.split(".")
    return any((host == name or host.endswith("." + name)) if "." in name else name in labels
               for name in FORBIDDEN_SOURCES)


def load_events(path: Path) -> list[Event]:
    """Events from data/events.json; [] when the file is missing, empty or broken (cached by
    mtime and size, so a rebuilt file is picked up without a restart)."""
    try:
        stat = path.stat()
    except OSError:
        return []
    stamp = (stat.st_mtime_ns, stat.st_size)
    hit = _CACHE.get(str(path))
    if hit is not None and hit[0] == stamp:
        return hit[1]
    events: list[Event] = []
    try:
        raw = json.loads(path.read_text(encoding="utf-8") or "[]")
    except (OSError, ValueError) as exc:
        log.warning("events file unreadable (%s): no events", type(exc).__name__)
        raw = []
    if not isinstance(raw, list):
        log.warning("events file is not a list: no events")
        raw = []
    skipped = 0
    for item in raw:
        try:
            events.append(Event.model_validate(item))
        except ValidationError:
            skipped += 1
    if skipped:
        log.warning("events: %d of %d records skipped (format)", skipped, len(raw))
    _CACHE[str(path)] = (stamp, events)
    return events


def assumed_end(event: Event) -> datetime:
    """ends_at, or NO_END_MIN after the start but not past the end of the start day."""
    if event.ends_at is not None:
        return event.ends_at
    start = event.starts_at.astimezone(ASTANA_TZ)
    midnight = (start + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return min(start + timedelta(minutes=NO_END_MIN), midnight)


def is_current(event: Event, now: datetime) -> bool:
    if now >= assumed_end(event):
        return False
    local_now = now.astimezone(ASTANA_TZ)
    start = event.starts_at.astimezone(ASTANA_TZ)
    return start.date() <= local_now.date() or start - local_now <= timedelta(hours=AHEAD_H)


def current(events: Sequence[Event], now: datetime) -> list[Event]:
    """Events not over at `now` that start today or within AHEAD_H hours, by start time."""
    return sorted((e for e in events if is_current(e, now)), key=lambda e: (e.starts_at, e.id))


def is_session(event: Event) -> bool:
    """One sitting (a concert, a show): no ends_at (then NO_END_MIN) or at most SESSION_MAX
    long. A longer event (an exhibition 01.10–30.11) is a run: its daily hours are not known."""
    return event.ends_at is None or event.ends_at - event.starts_at <= SESSION_MAX


def is_live(event: Event, now: datetime) -> bool:
    """«Идёт сейчас»: a session with both times known and starts_at ≤ now < ends_at. A run is
    never «on now» (at 23:00 the exhibition hall may be closed): it «runs until …»."""
    return (event.ends_at is not None and is_session(event)
            and event.starts_at <= now < event.ends_at)


def is_running(event: Event, now: datetime) -> bool:
    """A run (not a session) that has started and is not over: «проходит до 30.11 18:00»."""
    return (event.ends_at is not None and not is_session(event)
            and event.starts_at <= now < event.ends_at)


def is_event_id(poi_id: str) -> bool:
    return poi_id.startswith(EVENT_PREFIX)


_TEXTS: dict[str, dict[str, str]] = {
    "ru": {"live": "Идёт сейчас: {start}–{end}",
           "run": "Проходит до {end}, часы работы площадки не указаны",
           "start": "Начало в {start}", "start_day": "Начало {day} в {start}",
           "end": "окончание в {end}", "end_day": "окончание {day} в {end}",
           "src": "по данным источника, проверьте перед визитом",
           "reason_live": "идёт сейчас, до {end}", "reason_run": "проходит до {end}",
           "reason_start": "начало в {start}", "reason_start_day": "начало {day} в {start}"},
    "kk": {"live": "Қазір өтіп жатыр: {start}–{end}",
           "run": "{end} дейін өтеді, алаңның жұмыс уақыты көрсетілмеген",
           "start": "Басталуы: {start}", "start_day": "Басталуы: {day}, {start}",
           "end": "аяқталуы: {end}", "end_day": "аяқталуы: {day}, {end}",
           "src": "дереккөз бойынша, барар алдында тексеріңіз",
           "reason_live": "қазір өтіп жатыр, {end} дейін", "reason_run": "{end} дейін өтеді",
           "reason_start": "басталуы: {start}", "reason_start_day": "басталуы: {day}, {start}"},
    "en": {"live": "On now: {start}–{end}",
           "run": "Runs until {end}; venue hours not listed",
           "start": "Starts at {start}", "start_day": "Starts {day} at {start}",
           "end": "ends at {end}", "end_day": "ends {day} at {end}",
           "src": "per the source, check before you go",
           "reason_live": "on now, until {end}", "reason_run": "runs until {end}",
           "reason_start": "starts at {start}", "reason_start_day": "starts {day} at {start}"},
}


def _hm(value: datetime) -> str:
    return f"{value.astimezone(ASTANA_TZ):%H:%M}"


def _day(value: datetime) -> str:
    return f"{value.astimezone(ASTANA_TZ):%d.%m}"


def _today(value: datetime, now: datetime) -> bool:
    return value.astimezone(ASTANA_TZ).date() == now.astimezone(ASTANA_TZ).date()


def _clock(value: datetime, now: datetime) -> str:
    """«19:00» today, «30.11 18:00» on another day (local time): a time is never shown
    without its date when the date is not today."""
    return _hm(value) if _today(value, now) else f"{_day(value)} {_hm(value)}"


def _dated(t: dict[str, str], key: str, value: datetime, now: datetime) -> str:
    """t[key] for today, t[key + "_day"] with the date otherwise."""
    name = "start" if key.endswith("start") else "end"
    if _today(value, now):
        return t[key].format(**{name: _hm(value)})
    return t[f"{key}_day"].format(**{name: _hm(value)}, day=_day(value))


def note(event: Event, now: datetime, lang: Lang) -> str:
    """The card's open_note for an event, written by the code (never «open now»)."""
    t = _TEXTS.get(lang, _TEXTS["ru"])
    if is_live(event, now):
        assert event.ends_at is not None
        text = t["live"].format(start=_clock(event.starts_at, now),
                                end=_clock(event.ends_at, now))
    elif is_running(event, now):
        assert event.ends_at is not None
        text = t["run"].format(end=_clock(event.ends_at, now))
    else:
        text = _dated(t, "start", event.starts_at, now)
        if event.ends_at is not None:
            text += "; " + _dated(t, "end", event.ends_at, now)
    return f"{text} — {t['src']}."


def reason(event: Event, now: datetime, lang: Lang) -> str:
    """Short reason for the template why: «начало в 19:00» / «идёт сейчас, до 21:00» /
    «проходит до 30.11 18:00»."""
    t = _TEXTS.get(lang, _TEXTS["ru"])
    if is_live(event, now):
        assert event.ends_at is not None
        return t["reason_live"].format(end=_clock(event.ends_at, now))
    if is_running(event, now):
        assert event.ends_at is not None
        return t["reason_run"].format(end=_clock(event.ends_at, now))
    return _dated(t, "reason_start", event.starts_at, now)


def visit_minutes(event: Event, now: datetime) -> tuple[int, int] | None:
    """(low, high) on site from the times of a session (what is left of it); None for a run
    or without ends_at (the category estimate is used then)."""
    if event.ends_at is None or not is_session(event):
        return None
    left = (event.ends_at - max(now, event.starts_at)).total_seconds() / 60
    minutes = max(5, min(300, round(left)))
    return minutes, minutes


def as_poi(event: Event, lang: Lang) -> POI:
    """The event as a search record: same filters and scoring as a place."""
    tags = sorted(set(event.tags) & TAG_VOCAB)
    return POI(
        id=event.id,
        name=event.title_in(lang),
        name_i18n={k: v for k, v in event.title.items() if k in ("ru", "kk", "en") and v},
        category=event.category,
        tags=tags,
        geo=event.geo,
        address=event.place_line(),
        indoor=event.indoor,
        kids_friendly=event.kids_ok,
        popularity=0.0,
        sources=["manual"],
        data_confidence=DATA_CONFIDENCE,
        fetched_at=event.fetched_at or f"{event.starts_at:%Y-%m-%d}",
    )


def verdict(event: Event, now: datetime, lang: Lang) -> HoursVerdict:
    """unknown / low: the venue's hours are not known; the note gives the event time."""
    status: Literal["unknown"] = "unknown"
    return HoursVerdict(status=status, confidence="low", note=note(event, now, lang))
