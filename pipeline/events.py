"""Events added by hand: data/manual/events.json -> checked, venues bound -> data/events.json.

    python -m pipeline.events            # check, bind venues, write data/events.json
    python -m pipeline.events --check    # check only, write nothing; exit code 1 on errors
    python -m pipeline.events --schema   # rewrite data/manual/events.schema.json

People add events from the official site of a venue or an organiser: facts and a link only,
no descriptions, so nothing is retold (an unknown field such as "description" is an error).
A source's domain must first be listed in data/manual/event_sources.json with the page of
its terms that allows storing these facts; 2GIS and Yandex are refused even if listed.

The venue is given by our POI id or by coordinates. Coordinates are bound to a POI by the
venue name within 300 m, or, without a name, to the nearest POI within 50 m; an event that
binds to nothing keeps its own point. data/events.json (format: docs/ARCHITECTURE.md,
section 5) lists the events that have not ended yet, by start time; times are UTC+5.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    ValidationError,
    field_validator,
    model_validator,
)

from app.config import get_settings
from app.hours import ASTANA_TZ
from app.schemas import POI, TAG_VOCAB, Category, GeoPoint
from pipeline.fetch_osm import ASTANA_BBOX
from pipeline.normalize import contains_name, haversine_m, name_keys, same_name

FORMAT_VERSION = 1
BIND_BY_NAME_M = 300.0  # the venue name matches a POI this close
BIND_NEAREST_M = 50.0  # no venue name: the nearest POI this close
ID_GEO_MAX_M = 300.0  # venue_poi_id and geo both given: they must agree
MAX_DURATION = timedelta(days=366)  # a longer event is a typo in a date
# their terms forbid storing their data (CLAUDE.md): never a source, even if listed
FORBIDDEN_HOSTS = ("2gis", "yandex", "ya.ru")
NOT_VENUES = frozenset({"playground"})  # an unnamed «Детская площадка» is not a venue
_NON_WORD = re.compile(r"[\W_]+")

Slug = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9-]{2,79}$")]
NonNegative = Annotated[StrictInt, Field(ge=0)]


class Title(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    ru: str = Field(min_length=2, max_length=160)
    kk: str | None = Field(default=None, min_length=2, max_length=160)
    en: str | None = Field(default=None, min_length=2, max_length=160)


class PriceRange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    min: NonNegative
    max: NonNegative | None = None

    @model_validator(mode="after")
    def _ordered(self) -> PriceRange:
        if self.max is not None and self.max < self.min:
            raise ValueError("price.max меньше price.min")
        return self


class EventIn(BaseModel):
    """One event as a person writes it in data/manual/events.json."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    id: Slug = Field(description="латиница, цифры и дефис: opera-tosca-2026-11-01")
    title: str | Title = Field(description="строка по-русски или {ru, kk, en}")
    category: Category = Field(description="категория места, как у POI: theatre, museum...")
    starts_at: AwareDatetime = Field(description="ISO с поясом: 2026-11-01T19:00:00+05:00")
    ends_at: AwareDatetime | None = Field(default=None, description="если известно")
    venue_poi_id: str | None = Field(default=None, description="id места из poi.jsonl")
    geo: GeoPoint | None = Field(default=None, description="точка площадки, если нет id")
    venue: str | None = Field(default=None, min_length=2, max_length=160,
                              description="название площадки")
    address: str | None = Field(default=None, min_length=2, max_length=200)
    tags: list[str] = Field(default_factory=list, description="только из TAG_VOCAB")
    price: NonNegative | PriceRange | None = Field(
        default=None, description="тенге: 0 — бесплатно, 3000 или {min, max}; нет — null")
    indoor: StrictBool | None = None
    kids_ok: StrictBool | None = None
    source_url: str = Field(max_length=2048, description="https-страница события у источника")
    fetched_at: date = Field(description="когда сверено с источником: 2026-10-20")

    @field_validator("title")
    @classmethod
    def _title_line(cls, value: str | Title) -> str | Title:
        text = value if isinstance(value, str) else value.ru
        if not 2 <= len(text) <= 160 or "\n" in text:
            raise ValueError("название — одна строка, 2–160 знаков")
        return value

    @field_validator("tags")
    @classmethod
    def _vocab(cls, value: list[str]) -> list[str]:
        unknown = sorted(set(value) - TAG_VOCAB)
        if unknown:
            raise ValueError(f"теги не из TAG_VOCAB: {', '.join(unknown)}")
        return sorted(set(value))

    @field_validator("source_url")
    @classmethod
    def _https(cls, value: str) -> str:
        parts = urlsplit(value)
        if parts.scheme != "https" or not parts.hostname or " " in value:
            raise ValueError("нужна ссылка https://")
        if any(bad in parts.hostname for bad in FORBIDDEN_HOSTS):
            raise ValueError("2GIS и Яндекс запрещены как источник: их условия не разрешают "
                             "хранить данные")
        return value

    @model_validator(mode="after")
    def _venue_and_dates(self) -> EventIn:
        if not self.venue_poi_id and self.geo is None:
            raise ValueError("нужна площадка: venue_poi_id или geo")
        if self.ends_at is not None:
            if self.ends_at <= self.starts_at:
                raise ValueError("ends_at должен быть позже starts_at")
            if self.ends_at - self.starts_at > MAX_DURATION:
                raise ValueError("событие длиннее года: проверьте даты")
        return self


class EventsFile(BaseModel):
    """data/manual/events.json as a whole (only for the JSON schema editors check against)."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    schema_ref: str | None = Field(default=None, alias="$schema")
    format_version: Literal[1]
    events: list[EventIn]


class Source(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    domain: str = Field(pattern=r"^[a-z0-9.-]+\.[a-z]{2,}$")
    name: str = Field(min_length=2, max_length=120)
    terms_url: str = Field(pattern=r"^https://\S+$")
    checked_at: date
    note: str | None = None

    @field_validator("domain")
    @classmethod
    def _allowed(cls, value: str) -> str:
        if any(bad in value for bad in FORBIDDEN_HOSTS):
            raise ValueError("2GIS и Яндекс нельзя: их условия запрещают хранить данные")
        return value


@dataclass
class Venue:
    poi: POI | None
    bound_by: Literal["id", "name", "nearest"] | None
    distance_m: float | None


@dataclass
class CheckResult:
    events: list[dict] = field(default_factory=list)  # upcoming, in the data/events.json form
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    total: int = 0
    valid: int = 0
    past: int = 0
    bound_by: Counter = field(default_factory=Counter)  # id / name / nearest / none
    by_source: Counter = field(default_factory=Counter)
    readable: bool = True  # False: a manual file is not valid JSON -> keep data/events.json

    def summary(self) -> dict:
        return {
            "total": self.total, "valid": self.valid, "upcoming": len(self.events),
            "past": self.past, "bound_by": dict(self.bound_by),
            "by_source": dict(self.by_source.most_common()),
            "errors": self.errors, "warnings": self.warnings,
        }


def _keys(name: str) -> set[str]:
    key = _NON_WORD.sub("", name.casefold())
    return {key} if key else set()


def bind_venue(event: EventIn, pois: Sequence[POI], by_id: Mapping[str, POI]) -> Venue:
    """Raises ValueError when the venue contradicts our data."""
    if event.venue_poi_id:
        poi = by_id.get(event.venue_poi_id)
        if poi is None:
            raise ValueError(f"venue_poi_id {event.venue_poi_id}: нет в data/processed/poi.jsonl")
        dist = None
        if event.geo is not None:
            dist = haversine_m(event.geo.lat, event.geo.lon, poi.geo.lat, poi.geo.lon)
            if dist > ID_GEO_MAX_M:
                raise ValueError(f"venue_poi_id и geo расходятся на {dist:.0f} м")
        return Venue(poi, "id", round(dist, 1) if dist is not None else None)
    assert event.geo is not None
    lat, lon = event.geo.lat, event.geo.lon
    south, west, north, east = ASTANA_BBOX
    if not (south <= lat <= north and west <= lon <= east):
        raise ValueError(f"geo {lat}, {lon} вне Астаны")
    near = sorted(
        (d, p.id, p) for p in pois
        if (d := haversine_m(lat, lon, p.geo.lat, p.geo.lon)) <= BIND_BY_NAME_M
    )
    if event.venue:
        wanted = _keys(event.venue)
        for d, _, p in near:
            keys = name_keys(p)
            if same_name(wanted, keys) or contains_name(wanted, keys):
                return Venue(p, "name", round(d, 1))
        return Venue(None, None, None)  # a named venue we do not have: no guessing
    for d, _, p in near:
        if d > BIND_NEAREST_M:
            break
        if p.category not in NOT_VENUES:
            return Venue(p, "nearest", round(d, 1))
    return Venue(None, None, None)


def _price(price: int | PriceRange | None) -> dict | None:
    if price is None:
        return None
    if isinstance(price, int):
        return {"min": price, "max": price, "currency": "KZT"}
    return {"min": price.min, "max": price.max, "currency": "KZT"}


def to_record(event: EventIn, venue: Venue, source_name: str) -> dict:
    """The data/events.json object (docs/ARCHITECTURE.md, section 5) + price/indoor/kids_ok."""
    title = Title(ru=event.title) if isinstance(event.title, str) else event.title
    poi = venue.poi
    geo = poi.geo if poi else event.geo
    assert geo is not None
    indoor = event.indoor if event.indoor is not None else (poi.indoor if poi else None)
    tags = set(event.tags)
    if indoor is not None:
        tags.add("indoor" if indoor else "outdoor")
    if event.kids_ok:
        tags.add("kids")
    price = _price(event.price)
    if price and price["max"] == 0:
        tags.add("free")
    return {
        "id": f"event:{event.id}",
        "title": title.model_dump(exclude_none=True),
        "category": event.category,
        "tags": sorted(tags & TAG_VOCAB),
        "starts_at": event.starts_at.astimezone(ASTANA_TZ).isoformat(),
        "ends_at": event.ends_at.astimezone(ASTANA_TZ).isoformat() if event.ends_at else None,
        "venue": event.venue or (poi.name if poi else None),
        "address": event.address or (poi.address if poi else None),
        "geo": {"lat": geo.lat, "lon": geo.lon},
        "venue_poi_id": poi.id if poi else None,
        "venue_bound_by": venue.bound_by,
        "source_url": event.source_url,
        "source_name": source_name,
        "fetched_at": event.fetched_at.isoformat(),
        "price": price,
        "indoor": indoor,
        "kids_ok": event.kids_ok,
    }


def _source_of(url: str, sources: Sequence[Source]) -> Source | None:
    host = urlsplit(url).hostname or ""
    return next((s for s in sources if host == s.domain or host.endswith("." + s.domain)), None)


def _error_text(err: Mapping) -> str:
    where = ".".join(str(x) for x in err.get("loc", ()) if not isinstance(x, int)) or "событие"
    kind = err.get("type", "")
    if kind == "extra_forbidden":
        return f"{where}: лишнее поле (храним только факты и ссылку, описаний нет)"
    if kind == "missing":
        return f"{where}: обязательное поле"
    if kind == "timezone_aware":
        return f"{where}: нужен часовой пояс, например 2026-11-01T19:00:00+05:00"
    msg = str(err.get("msg", "")).removeprefix("Value error, ")
    return f"{where}: {msg}"


def check(
    raw: object, pois: Sequence[POI], sources_raw: object, now: datetime
) -> CheckResult:
    res = CheckResult()
    sources: list[Source] = []
    if not isinstance(sources_raw, dict) or not isinstance(sources_raw.get("sources"), list):
        res.errors.append('event_sources.json: нужен объект {"format_version": 1, "sources": []}')
    else:
        for i, item in enumerate(sources_raw["sources"], 1):
            try:
                sources.append(Source.model_validate(item))
            except ValidationError as exc:
                res.errors += [f"источник #{i}: {_error_text(e)}" for e in exc.errors()]
    if not isinstance(raw, dict) or not isinstance(raw.get("events"), list):
        res.errors.append('events.json: нужен объект {"format_version": 1, "events": [...]}')
        return res
    extra = set(raw) - {"$schema", "format_version", "events"}
    if raw.get("format_version") != FORMAT_VERSION or extra:
        res.errors.append(f"events.json: format_version должен быть {FORMAT_VERSION}"
                          + (f", лишние ключи: {', '.join(sorted(extra))}" if extra else ""))
    by_id = {p.id: p for p in pois}
    seen: dict[str, int] = {}
    res.total = len(raw["events"])
    for i, item in enumerate(raw["events"], 1):
        label = f"#{i}" + (f" {item['id']}" if isinstance(item, dict)
                           and isinstance(item.get("id"), str) else "")
        try:
            event = EventIn.model_validate(item)
        except ValidationError as exc:
            res.errors += [f"{label}: {_error_text(e)}" for e in exc.errors()]
            continue
        if event.id in seen:
            res.errors.append(f"{label}: id уже есть у события #{seen[event.id]}")
            continue
        seen[event.id] = i
        source = _source_of(event.source_url, sources)
        if source is None:
            res.errors.append(f"{label}: домен {urlsplit(event.source_url).hostname} не в "
                              "data/manual/event_sources.json (сначала проверьте его условия)")
            continue
        try:
            venue = bind_venue(event, pois, by_id)
        except ValueError as exc:
            res.errors.append(f"{label}: {exc}")
            continue
        res.valid += 1
        res.bound_by[venue.bound_by or "none"] += 1
        for value in (event.starts_at, event.ends_at):
            if value is not None and value.utcoffset() != timedelta(hours=5):
                res.warnings.append(f"{label}: пояс не +05:00 (в Астане UTC+5 с 2024 года), "
                                    "время переведено — проверьте")
                break
        if venue.poi is None:
            res.warnings.append(f"{label}: площадка не привязана к POI, точка — из geo")
        if (event.ends_at or event.starts_at) < now:
            res.past += 1
            res.warnings.append(f"{label}: уже прошло, в data/events.json не попадает")
            continue
        if event.starts_at - now > MAX_DURATION:
            res.warnings.append(f"{label}: начало больше чем через год — проверьте год")
        res.by_source[source.name] += 1
        res.events.append(to_record(event, venue, source.name))
    res.events.sort(key=lambda e: (e["starts_at"], e["id"]))
    return res


def _load_json(path: Path, default: object) -> tuple[object, str | None]:
    """(data, None), or (None, why) when the file is not readable JSON."""
    if not path.exists():
        return default, None
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return None, f"{path.name}: не читается как JSON ({exc})"


def check_files(now: datetime | None = None) -> CheckResult:
    settings = get_settings()
    manual = settings.data_dir / "manual"
    with settings.poi_path.open(encoding="utf-8") as fh:
        pois = [POI.model_validate_json(line) for line in fh if line.strip()]
    raw, raw_error = _load_json(
        manual / "events.json", {"format_version": FORMAT_VERSION, "events": []})
    sources, sources_error = _load_json(
        manual / "event_sources.json", {"format_version": 1, "sources": []})
    unreadable = [e for e in (raw_error, sources_error) if e]
    if unreadable:
        return CheckResult(errors=unreadable, readable=False)
    return check(raw, pois, sources, now or datetime.now(ASTANA_TZ))


def write_events(events: Sequence[Mapping], path: Path) -> None:
    path.write_text(json.dumps(list(events), ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")


def run(now: datetime | None = None) -> CheckResult:
    """Check and write data/events.json (only the valid upcoming events). A manual file that
    is not JSON at all leaves the published data/events.json as it was."""
    res = check_files(now)
    if res.readable:
        write_events(res.events, get_settings().data_dir / "events.json")
    return res


def json_schema() -> dict:
    schema = EventsFile.model_json_schema(by_alias=True)
    schema["title"] = "Qadam: события, внесённые вручную (data/manual/events.json)"
    return schema


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="check only, write nothing")
    parser.add_argument("--schema", action="store_true", help="rewrite events.schema.json")
    args = parser.parse_args(argv)
    if args.schema:
        out = get_settings().data_dir / "manual" / "events.schema.json"
        out.write_text(json.dumps(json_schema(), ensure_ascii=False, indent=2) + "\n",
                       encoding="utf-8")
        print(f"schema -> {out}")
        return
    res = check_files() if args.check else run()
    for line in res.errors:
        print(f"ОШИБКА {line}")
    for line in res.warnings:
        print(f"внимание {line}")
    print(json.dumps({k: v for k, v in res.summary().items() if k not in ("errors", "warnings")},
                     ensure_ascii=False))
    if res.errors:
        sys.exit(1)


if __name__ == "__main__":
    main()
