"""app/events.py: data/events.json -> current events, honest times (T123, no network)."""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from app import events
from app.hours import ASTANA_TZ
from app.validate import has_live_claim, has_open_claim

# the honest tail of an event note (app.events)
CHECK_RU = " — по данным источника, проверьте перед визитом."
CHECK_KK = " — дереккөз бойынша, барар алдында тексеріңіз."
CHECK_EN = " — per the source, check before you go."

AT_17 = datetime(2026, 10, 24, 17, 0, tzinfo=ASTANA_TZ)


def record(slug: str = "tosca", starts: str = "2026-10-24T19:00:00+05:00",
           ends: str | None = "2026-10-24T21:30:00+05:00", **kw: Any) -> dict[str, Any]:
    """One object of data/events.json, as pipeline.events writes it."""
    data: dict[str, Any] = {
        "id": f"event:{slug}",
        "title": {"ru": "Опера «Тоска»", "en": "Tosca (opera)"},
        "category": "theatre",
        "tags": ["indoor", "live_music"],
        "starts_at": starts,
        "ends_at": ends,
        "venue": "Астана Опера",
        "address": "ул. Кунаева, 1",
        "geo": {"lat": 51.1236, "lon": 71.4296},
        "venue_poi_id": "osm:way/1",
        "venue_bound_by": "id",
        "source_url": "https://astanaopera.kz/afisha/tosca",
        "source_name": "Астана Опера",
        "fetched_at": "2026-10-20",
        "price": {"min": 3000, "max": 15000, "currency": "KZT"},
        "indoor": True,
        "kids_ok": None,
    }
    data.update(kw)
    return data


def write(tmp_path: Path, items: object, name: str = "events.json") -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")
    return path


def event(**kw: Any) -> events.Event:
    return events.Event.model_validate(record(**kw))


# ------------------------------------------------------------------------- loading


def test_missing_empty_and_broken_files_give_no_events_quietly(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    assert events.load_events(tmp_path / "nope.json") == []
    assert events.load_events(write(tmp_path, [], "empty.json")) == []
    blank = tmp_path / "blank.json"
    blank.write_text("", encoding="utf-8")
    assert events.load_events(blank) == []
    broken = tmp_path / "broken.json"
    broken.write_text('[{"id": "event:x", "title": {"ru": "Секрет"', encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        assert events.load_events(broken) == []
        assert events.load_events(write(tmp_path, {"events": []}, "obj.json")) == []
    assert "Секрет" not in caplog.text  # counts and types only, never the content


def test_bad_records_are_skipped_good_ones_kept(tmp_path: Path,
                                               caplog: pytest.LogCaptureFixture) -> None:
    items = [
        record("good"),
        record("bad-tz", starts="2026-10-24T19:00:00"),  # no time zone
        record("bad-url", source_url="http://example.kz/x"),  # not https
        record("from-2gis", source_url="https://2gis.kz/astana/event/1"),  # forbidden source
        record("bad-order", ends="2026-10-24T18:00:00+05:00"),  # ends before it starts
        {"id": "event:no-geo", "title": {"ru": "Без площадки"}},
        "not an object",
    ]
    with caplog.at_level(logging.WARNING):
        got = events.load_events(write(tmp_path, items))
    assert [e.id for e in got] == ["event:good"]
    assert "6 of 7" in caplog.text and "Без площадки" not in caplog.text


def test_a_rebuilt_file_is_read_again(tmp_path: Path) -> None:
    path = write(tmp_path, [record("one")])
    assert [e.id for e in events.load_events(path)] == ["event:one"]
    write(tmp_path, [record("one"), record("two")])
    assert [e.id for e in events.load_events(path)] == ["event:one", "event:two"]


def test_the_repo_file_loads(tmp_path: Path) -> None:
    """data/events.json from DATA (now []) is read without errors."""
    from app.config import get_settings

    assert isinstance(events.load_events(get_settings().data_dir / "events.json"), list)


# ---------------------------------------------------------------- what is current


@pytest.mark.parametrize(("starts", "ends", "now", "shown"), [
    # ends known: until it is over
    ("2026-10-24T19:00:00+05:00", "2026-10-24T21:30:00+05:00", "2026-10-24T17:00", True),
    ("2026-10-24T19:00:00+05:00", "2026-10-24T21:30:00+05:00", "2026-10-24T21:00", True),
    ("2026-10-24T19:00:00+05:00", "2026-10-24T21:30:00+05:00", "2026-10-24T21:30", False),
    # no ends_at: 2 hours after the start…
    ("2026-10-24T19:00:00+05:00", None, "2026-10-24T20:59", True),
    ("2026-10-24T19:00:00+05:00", None, "2026-10-24T21:00", False),
    # …but never past the end of its day, and never an event of an earlier day
    ("2026-10-24T23:00:00+05:00", None, "2026-10-24T23:50", True),
    ("2026-10-24T23:00:00+05:00", None, "2026-10-25T00:10", False),
    # starts today (any time) or within 6 hours
    ("2026-10-24T22:00:00+05:00", None, "2026-10-24T08:00", True),
    ("2026-10-25T03:00:00+05:00", None, "2026-10-24T23:00", True),
    ("2026-10-25T19:00:00+05:00", None, "2026-10-24T17:00", False),
    ("2026-11-15T19:00:00+05:00", "2026-11-15T21:00:00+05:00", "2026-10-24T17:00", False),
    # a long exhibition that began days ago and is still on
    ("2026-10-01T10:00:00+05:00", "2026-11-30T18:00:00+05:00", "2026-10-24T17:00", True),
])
def test_current_events(starts: str, ends: str | None, now: str, shown: bool) -> None:
    at = datetime.fromisoformat(now).replace(tzinfo=ASTANA_TZ)
    assert [e.id for e in events.current([event(starts=starts, ends=ends)], at)] == (
        ["event:tosca"] if shown else [])


def test_current_compares_across_time_zones() -> None:
    """starts_at in UTC is the same moment: 14:00Z = 19:00 in Astana."""
    utc = event(starts="2026-10-24T14:00:00+00:00", ends="2026-10-24T16:30:00+00:00")
    assert events.is_live(utc, datetime(2026, 10, 24, 19, 30, tzinfo=ASTANA_TZ))
    assert events.note(utc, AT_17, "ru").startswith("Начало в 19:00")


def test_current_is_sorted_by_start() -> None:
    late = event(slug="late", starts="2026-10-24T20:00:00+05:00", ends=None)
    early = event(slug="early", starts="2026-10-24T18:00:00+05:00", ends=None)
    assert [e.id for e in events.current([late, early], AT_17)] == ["event:early", "event:late"]


# ------------------------------------------------------------------ honest times


def at(hhmm: str, day: int = 24) -> datetime:
    h, m = map(int, hhmm.split(":"))
    return datetime(2026, 10, day, h, m, tzinfo=ASTANA_TZ)


END = "2026-10-24T21:30:00+05:00"


@pytest.mark.parametrize(("ends", "now", "lang", "text"), [
    (END, "17:00", "ru", "Начало в 19:00; окончание в 21:30" + CHECK_RU),
    (END, "19:30", "ru", "Идёт сейчас: 19:00–21:30" + CHECK_RU),
    (None, "19:30", "ru", "Начало в 19:00" + CHECK_RU),  # no end: never «идёт сейчас»
    (END, "19:30", "kk", "Қазір өтіп жатыр: 19:00–21:30" + CHECK_KK),
    (None, "17:00", "kk", "Басталуы: 19:00 — дереккөз бойынша, барар алдында тексеріңіз."),
    (END, "19:30", "en", "On now: 19:00–21:30 — per the source, check before you go."),
    (END, "17:00", "en", "Starts at 19:00; ends at 21:30 — per the source, check before you go."),
])
def test_note_says_on_now_only_while_it_is_on(ends: str | None, now: str, lang: str,
                                              text: str) -> None:
    e = event(ends=ends)
    note = events.note(e, at(now), lang)  # type: ignore[arg-type]
    assert note == text
    assert not has_open_claim(note)  # an event time is not an «open now» claim
    assert has_live_claim(note) is events.is_live(e, at(now))


def test_note_and_reason_name_the_day_when_it_is_not_today() -> None:
    e = event(starts="2026-10-25T01:00:00+05:00", ends=None)
    assert events.note(e, at("23:00"), "ru") == "Начало 25.10 в 01:00" + CHECK_RU
    assert events.reason(e, at("23:00"), "en") == "starts 25.10 at 01:00"
    assert events.reason(event(), at("19:30"), "ru") == "идёт сейчас, до 21:30"
    assert events.reason(event(), at("17:00"), "kk") == "басталуы: 19:00"


def test_visit_minutes_from_the_event_times() -> None:
    assert events.visit_minutes(event(), at("17:00")) == (150, 150)
    assert events.visit_minutes(event(), at("21:00")) == (30, 30)  # what is left of it
    assert events.visit_minutes(event(ends=None), at("17:00")) is None
    long = event(starts="2026-10-01T10:00:00+05:00", ends="2026-11-30T18:00:00+05:00")
    assert events.visit_minutes(long, at("17:00")) is None  # a run: the category estimate


# ------------------------------------------------------------- as a search record


def test_as_poi_and_verdict() -> None:
    e = event(tags=["indoor", "live_music", "not_a_tag"])
    poi = events.as_poi(e, "en")
    assert poi.id == "event:tosca" and poi.name == "Tosca (opera)"
    assert poi.sources == ["manual"] and poi.category == "theatre"
    assert poi.address == "Астана Опера, ул. Кунаева, 1"
    assert poi.tags == ["indoor", "live_music"]  # TAG_VOCAB only
    assert events.as_poi(e, "kk").name == "Опера «Тоска»"  # no kk title: the Russian one
    v = events.verdict(e, at("17:00"), "ru")
    assert (v.status, v.confidence) == ("unknown", "low")
    assert v.note.startswith("Начало в 19:00")


def test_place_line_without_venue_or_address() -> None:
    assert event(venue=None).place_line() == "ул. Кунаева, 1"
    assert event(address=None).place_line() == "Астана Опера"
    assert event(venue=None, address=None).place_line() is None
    assert event(venue="Астана Опера, ул. Кунаева, 1").place_line() == (
        "Астана Опера, ул. Кунаева, 1")  # the address is already in the venue line


# ------------------------------------------- review: runs of many days, dates on other days

RUN = {"starts": "2026-10-01T10:00:00+05:00", "ends": "2026-11-30T18:00:00+05:00"}


@pytest.mark.parametrize(("now", "lang", "text"), [
    ("23:00", "ru", "Проходит до 30.11 18:00, часы работы площадки не указаны" + CHECK_RU),
    ("12:00", "ru", "Проходит до 30.11 18:00, часы работы площадки не указаны" + CHECK_RU),
    ("23:00", "kk", "30.11 18:00 дейін өтеді, алаңның жұмыс уақыты көрсетілмеген" + CHECK_KK),
    ("23:00", "en", "Runs until 30.11 18:00; venue hours not listed" + CHECK_EN),
])
def test_a_run_of_many_days_is_never_on_now(now: str, lang: str, text: str) -> None:
    """An exhibition 01.10–30.11 at 23:00: not «идёт сейчас: 10:00–18:00» (review BLOCK 2)."""
    e = event(**RUN)
    assert not events.is_session(e) and not events.is_live(e, at(now))
    assert events.is_running(e, at(now))
    note = events.note(e, at(now), lang)  # type: ignore[arg-type]
    assert note == text
    assert not has_live_claim(note) and not has_open_claim(note)
    assert events.reason(e, at(now), "ru") == "проходит до 30.11 18:00"
    assert events.reason(e, at(now), "en") == "runs until 30.11 18:00"


def test_times_on_another_day_carry_the_date() -> None:
    late = event(starts="2026-10-24T22:00:00+05:00", ends="2026-10-25T01:00:00+05:00")
    assert events.note(late, at("23:00"), "ru") == "Идёт сейчас: 22:00–25.10 01:00" + CHECK_RU
    assert events.reason(late, at("23:00"), "ru") == "идёт сейчас, до 25.10 01:00"
    after_midnight = at("00:30", day=25)
    assert events.note(late, after_midnight, "en") == "On now: 24.10 22:00–01:00" + CHECK_EN
    future_run = event(starts="2026-10-25T10:00:00+05:00", ends="2026-11-30T18:00:00+05:00")
    assert events.note(future_run, at("23:00"), "ru") == (
        "Начало 25.10 в 10:00; окончание 30.11 в 18:00" + CHECK_RU)
    assert events.note(future_run, at("23:00"), "kk") == (
        "Басталуы: 25.10, 10:00; аяқталуы: 30.11, 18:00" + CHECK_KK)
    assert events.note(future_run, at("23:00"), "en") == (
        "Starts 25.10 at 10:00; ends 30.11 at 18:00" + CHECK_EN)
    assert events.reason(future_run, at("23:00"), "ru") == "начало 25.10 в 10:00"


# --------------------------------- T133 (DATA ec50aa7): the forbidden sources, read once more


@pytest.mark.parametrize(("url", "refused"), [
    ("https://afisha.yandex.kz/astana/event/1", True),
    ("https://yandex.ru/afisha/1", True),
    ("https://go.2gis.com/x", True),
    ("https://ya.ru/x", True),
    ("https://ticketon.kz/astana/event/1", True),
    ("https://www.ticketon.kz/event/1", True),
    ("https://sxodim.com/astana/event/1", True),
    ("https://eventbrite.com/e/1", True),
    ("https://ticketon.com/e/1", True),  # a brand in any zone (DATA 7490099)
    ("https://kassir.kz/e/1", True),
    ("https://WWW.Kino.kz./film/1", True),  # normalised: case, www., trailing dot
    ("https://kinopark.kz/e/1", False),  # kino.kz is an exact domain
    ("https://afisha.astana.gov.kz/e/1", False),  # afisha.ru is an exact domain
    ("https://notyandex.kz/afisha/1", False),  # labels, not a substring
    ("https://my2gisfan.kz/e/1", False),
    ("https://notticketon.kz/e/1", False),
    ("https://astanaopera.kz/afisha/tosca", False),
])
def test_forbidden_sources_are_refused_by_host_labels(tmp_path: Path, url: str,
                                                      refused: bool) -> None:
    got = events.load_events(write(tmp_path, [record(source_url=url)]))
    assert (got == []) is refused


def test_the_reader_covers_the_data_lane_list() -> None:
    """pipeline.events.FORBIDDEN_SOURCES (DATA, T132) is the one list; this reader repeats it
    (pipeline/ is not in the server image) and must refuse every name of it."""
    from pipeline import events as data_events

    theirs = getattr(data_events, "FORBIDDEN_SOURCES", None)
    if theirs is None:
        pytest.skip("pipeline.events.FORBIDDEN_SOURCES not merged here yet")
    assert set(theirs) == events.FORBIDDEN_SOURCES
    for host in ("afisha.yandex.kz", "ticketon.com", "kassir.kz", "www.kino.kz.", "notyandex.kz",
                 "kinopark.kz", "afisha.astana.gov.kz", "astanaopera.kz"):
        assert events.forbidden_source(host) is (data_events.forbidden_source(host) is not None)


def test_source_kind_is_read_when_given(tmp_path: Path) -> None:
    got = events.load_events(write(tmp_path, [record("one", source_kind="venue"),
                                              record("two")]))
    assert [e.source_kind for e in got] == ["venue", None]
    assert events.load_events(write(tmp_path, [record(source_kind="aggregator")])) == []
