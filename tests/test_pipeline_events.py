"""pipeline.events: the hand-added events check and venue binding (no network)."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from app.hours import ASTANA_TZ
from app.schemas import POI
from pipeline.events import MAPS, TICKETS, check, forbidden_source, json_schemas

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 10, 20, 12, 0, tzinfo=ASTANA_TZ)
DLAT = 0.0009  # ~100 m


def poi(pid: str, name: str, category: str, lat: float, lon: float, **extra) -> POI:
    return POI.model_validate({
        "id": pid, "name": name, "category": category, "geo": {"lat": lat, "lon": lon},
        "sources": ["osm"], "data_confidence": 0.6, "fetched_at": "2026-10-18", **extra})


POIS = [
    poi("osm:way/1", "Астана Опера", "theatre", 51.1300, 71.4100, indoor=True,
        address="ул. Кунаева, 1"),
    poi("osm:node/2", "Кофе у оперы", "cafe", 51.1300 - 0.2 * DLAT, 71.4100 + 0.0005,
        indoor=True),
    poi("osm:node/3", "Детская площадка", "playground", 51.1500, 71.4300, indoor=False),
    poi("osm:node/4", "Сквер", "park", 51.1500 + 0.3 * DLAT, 71.4300, indoor=False),
]
SOURCES = {"format_version": 1, "sources": [
    {"domain": "astanaopera.kz", "name": "Astana Opera", "kind": "venue",
     "terms_url": "https://astanaopera.kz/terms", "checked_at": "2026-10-20"},
]}


def event(**over) -> dict:
    base = {
        "id": "tosca-2026-11-01",
        "title": "Тоска",
        "category": "theatre",
        "starts_at": "2026-11-01T19:00:00+05:00",
        "ends_at": "2026-11-01T22:00:00+05:00",
        "venue_poi_id": "osm:way/1",
        "source_url": "https://astanaopera.kz/afisha/tosca",
        "fetched_at": "2026-10-20",
    }
    return {k: v for k, v in (base | over).items() if v is not None}


def run(*events: dict, sources: dict = SOURCES):
    return check({"format_version": 1, "events": list(events)}, POIS, sources, NOW)


def test_event_by_poi_id_gets_the_contract_fields() -> None:
    res = run(event(price=0, kids_ok=True, title={"ru": "Тоска", "en": "Tosca"}))
    assert res.errors == [] and res.valid == 1
    [rec] = res.events
    assert rec["id"] == "event:tosca-2026-11-01"
    assert rec["title"] == {"ru": "Тоска", "en": "Tosca"}
    assert rec["starts_at"] == "2026-11-01T19:00:00+05:00"
    assert rec["venue"] == "Астана Опера" and rec["address"] == "ул. Кунаева, 1"
    assert rec["geo"] == {"lat": 51.13, "lon": 71.41} and rec["venue_poi_id"] == "osm:way/1"
    assert rec["source_name"] == "Astana Opera" and rec["fetched_at"] == "2026-10-20"
    assert rec["indoor"] is True  # from the venue
    assert rec["tags"] == ["free", "indoor", "kids"]
    assert rec["price"] == {"min": 0, "max": 0, "currency": "KZT"}
    assert rec["source_kind"] == "venue"
    assert res.summary()["by_source"] == {"Astana Opera": 1}
    assert res.summary()["by_kind"] == {"venue": 1}


def test_coordinates_bind_by_venue_name_before_a_nearer_place() -> None:
    near_cafe = {"lat": 51.1300 - 0.2 * DLAT, "lon": 71.4100 + 0.0004}  # ~5 m from the cafe
    res = run(event(venue_poi_id=None, geo=near_cafe, venue="Astana Opera / Астана Опера"))
    # the name variant "астанаопера" is inside the given one
    assert res.errors == [] and res.events[0]["venue_poi_id"] == "osm:way/1"
    assert res.events[0]["venue_bound_by"] == "name"
    assert res.events[0]["venue"] == "Astana Opera / Астана Опера"  # the person's wording


def test_coordinates_without_a_name_bind_only_to_a_close_venue() -> None:
    at_square = {"lat": 51.1500 + 0.25 * DLAT, "lon": 71.4300}  # 5 m from the square
    res = run(event(venue_poi_id=None, geo=at_square, category="park"))
    assert res.events[0]["venue_poi_id"] == "osm:node/4"
    at_playground = {"lat": 51.1500, "lon": 71.4300 + 0.0001}  # the playground is no venue
    res = run(event(venue_poi_id=None, geo=at_playground, category="park"))
    assert res.events[0]["venue_poi_id"] == "osm:node/4"  # the square, 27 m away
    far = {"lat": 51.1700, "lon": 71.4500}
    res = run(event(venue_poi_id=None, geo=far, venue="Новая площадка", category="other"))
    assert res.errors == [] and res.events[0]["venue_poi_id"] is None
    assert res.events[0]["geo"] == far and res.bound_by["none"] == 1
    assert any("не привязана" in w for w in res.warnings)


def test_a_named_venue_we_do_not_have_is_not_guessed() -> None:
    res = run(event(venue_poi_id=None, geo={"lat": 51.1300, "lon": 71.4100}, venue="Барыс Арена"))
    assert res.events[0]["venue_poi_id"] is None


@pytest.mark.parametrize(("over", "fragment"), [
    ({"venue_poi_id": "osm:node/999"}, "нет в data/processed/poi.jsonl"),
    ({"venue_poi_id": None}, "venue_poi_id или geo"),
    ({"starts_at": "2026-11-01T19:00:00"}, "часовой пояс"),
    ({"ends_at": "2026-11-01T18:00:00+05:00"}, "позже starts_at"),
    ({"ends_at": "2028-11-01T18:00:00+05:00"}, "длиннее года"),
    ({"description": "Опера Пуччини"}, "описаний нет"),
    ({"source_url": "http://astanaopera.kz/afisha"}, "https"),
    ({"source_url": "https://2gis.kz/astana/firm/1"}, "2GIS и Яндекс"),
    ({"source_url": "https://afisha.yandex.kz/astana"}, "2GIS и Яндекс"),
    ({"source_url": "https://www.ticketon.kz/event/1"}, "агрегаторы билетов"),
    ({"source_url": "https://other-venue.kz/event/1"}, "не в data/manual/event_sources.json"),
    ({"tags": ["kids", "romantic"]}, "не из TAG_VOCAB: romantic"),
    ({"category": "concert"}, "category"),
    ({"price": {"min": 5000, "max": 3000}}, "price.max меньше"),
    ({"price": True}, "price"),
    ({"id": "Tosca 1"}, "id"),
    ({"venue_poi_id": None, "geo": {"lat": 43.25, "lon": 76.95}}, "вне Астаны"),
    ({"geo": {"lat": 51.1300 + 5 * DLAT, "lon": 71.4100}}, "расходятся"),
    ({"fetched_at": None}, "fetched_at: обязательное поле"),
])
def test_bad_events_are_errors(over: dict, fragment: str) -> None:
    res = run(event(**over))
    assert res.valid == 0 and res.events == []
    assert any(fragment in e for e in res.errors), res.errors


def test_duplicates_past_events_and_other_time_zones() -> None:
    past = event(id="old-show", starts_at="2026-10-01T19:00:00+05:00",
                 ends_at="2026-10-01T21:00:00+05:00")
    almaty_style = event(id="tz-show", starts_at="2026-11-02T20:00:00+06:00", ends_at=None)
    res = run(event(), event(), past, almaty_style)
    assert any("id уже есть у события #1" in e for e in res.errors)
    assert res.past == 1 and any("уже прошло" in w for w in res.warnings)
    assert any("пояс не +05:00" in w for w in res.warnings)
    assert [e["id"] for e in res.events] == ["event:tosca-2026-11-01", "event:tz-show"]
    assert res.events[1]["starts_at"] == "2026-11-02T19:00:00+05:00"  # the same instant
    assert res.events[1]["ends_at"] is None


def test_forbidden_or_broken_sources_are_errors() -> None:
    bad = {"format_version": 1, "sources": [
        {"domain": "2gis.kz", "name": "2GIS", "terms_url": "https://2gis.kz/terms",
         "checked_at": "2026-10-20"}]}
    res = run(event(), sources=bad)
    assert any("источник #1" in e and "2GIS" in e for e in res.errors)
    assert check({"events": "no"}, POIS, SOURCES, NOW).errors[0].startswith("events.json")


def test_committed_files_check_clean_and_the_schema_is_current() -> None:
    from pipeline.events import FORMAT_VERSION

    manual = ROOT / "data" / "manual"
    raw = json.loads((manual / "events.json").read_text(encoding="utf-8"))
    sources = json.loads((manual / "event_sources.json").read_text(encoding="utf-8"))
    pois = [POI.model_validate_json(line) for line in
            (ROOT / "data" / "processed" / "poi.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()]
    res = check(raw, pois, sources, NOW)
    assert raw["format_version"] == FORMAT_VERSION and res.errors == []
    for name, schema in json_schemas().items():
        committed = json.loads((manual / name).read_text(encoding="utf-8"))
        assert committed == schema, f"{name}: run python -m pipeline.events --schema"
    published = json.loads((ROOT / "data" / "events.json").read_text(encoding="utf-8"))
    assert isinstance(published, list)


@pytest.mark.parametrize(("host", "reason"), [
    ("2gis.kz", MAPS), ("2gis.ru", MAPS), ("afisha.yandex.kz", MAPS), ("ya.ru", MAPS),
    ("ticketon.kz", TICKETS), ("www.ticketon.kz", TICKETS), ("sxodim.com", TICKETS),
    ("ticketon.com", TICKETS), ("kassir.kz", TICKETS), ("kino.kz", TICKETS),
    ("www.kino.kz", TICKETS), ("afisha.ru", TICKETS),
    ("astanaopera.kz", None), ("notyandex.kz", None), ("kinopark.kz", None),
    ("afisha.astana.gov.kz", None),
    ("ticketon.kz.example.com", TICKETS),  # a brand label anywhere in the host: refused
])
def test_forbidden_sources_one_list(host: str, reason: str | None) -> None:
    assert forbidden_source(host) == reason


def source(**over) -> dict:
    base = {"domain": "astanaopera.kz", "name": "Astana Opera", "kind": "venue",
            "terms_url": "https://astanaopera.kz/terms", "checked_at": "2026-10-20"}
    return {k: v for k, v in (base | over).items() if v is not None}


@pytest.mark.parametrize(("sources", "fragment"), [
    ([source(kind=None)], "источник #1: kind: обязательное поле"),
    ([source(kind="aggregator")], "источник #1: kind"),
    ([source(domain="ticketon.kz", name="Ticketon", kind="organizer")], "агрегаторы билетов"),
    ([source(domain="afisha.yandex.kz", name="Афиша")], "2GIS и Яндекс"),
    ([source(), source(name="Опера ещё раз")], "источник #2: домен astanaopera.kz уже в списке"),
    ([source(), source(domain="WWW.AstanaOpera.KZ")], "источник #2: домен astanaopera.kz уже"),
    ([source(domain="kassir.kz", name="Kassir", kind="organizer")], "агрегаторы билетов"),
])
def test_bad_sources_are_errors(sources: list[dict], fragment: str) -> None:
    res = run(event(), sources={"format_version": 1, "sources": sources})
    assert any(fragment in e for e in res.errors), res.errors


@pytest.mark.parametrize("kind", ["venue", "organizer", "city_portal"])
def test_every_source_kind_is_accepted(kind: str) -> None:
    portal = source(domain="astana.gov.kz", name="Акимат Астаны", kind=kind,
                    terms_url="https://astana.gov.kz/terms")
    res = run(event(source_url="https://astana.gov.kz/afisha/1"),
              sources={"format_version": 1, "sources": [portal]})
    assert res.errors == [] and res.events[0]["source_kind"] == kind
    assert res.events[0]["source_name"] == "Акимат Астаны"


def test_a_source_domain_is_normalised() -> None:
    upper = source(domain=" WWW.AstanaOpera.KZ. ")
    res = run(event(), sources={"format_version": 1, "sources": [upper]})
    assert res.errors == [] and res.events[0]["source_name"] == "Astana Opera"
