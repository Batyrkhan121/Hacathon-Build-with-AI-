"""T21: OSM opening_hours -> HoursVerdict.

Semantics (OSM spec): days not covered by any rule are closed. Today described explicitly ->
confidence high; today not described while other days are -> closed/medium. Anything we do
not understand -> unknown/low, never a guessed "open". PH/SH rules are ignored (not a holiday).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.hours import ASTANA_TZ, evaluate_hours, now_astana

from .conftest import ROOT, read_jsonl

# Week of 19.10.2026: Monday .. Sunday
DAY = {"Mo": 19, "Tu": 20, "We": 21, "Th": 22, "Fr": 23, "Sa": 24, "Su": 25}


def at(day: str, hh: int, mm: int = 0) -> datetime:
    return datetime(2026, 10, DAY[day], hh, mm, tzinfo=ASTANA_TZ)


# 1-2. no data
@pytest.mark.parametrize("raw", [None, "", "   "])
def test_no_data_is_unknown_low(raw: str | None) -> None:
    v = evaluate_hours(raw, at("Mo", 12))
    assert (v.status, v.confidence) == ("unknown", "low")
    assert v.note == "нет данных о часах работы"
    assert v.closes_in_min is None and v.opens_in_min is None


# 3. 24/7
def test_24_7() -> None:
    v = evaluate_hours("24/7", at("Tu", 3))
    assert (v.status, v.confidence) == ("open", "high")
    assert v.closes_in_min is None
    assert "круглосуточно" in v.note


# 4. daily, open
def test_daily_open_with_note() -> None:
    v = evaluate_hours("Mo-Su 10:00-22:00", at("Mo", 12))
    assert (v.status, v.confidence) == ("open", "high")
    assert v.closes_in_min == 600
    assert "по данным OSM: ежедневно 10:00–22:00" in v.note


# 5. daily, before opening
def test_daily_before_opening() -> None:
    v = evaluate_hours("Mo-Su 10:00-22:00", at("Mo", 9, 20))
    assert (v.status, v.confidence) == ("closed", "high")
    assert v.opens_in_min == 40
    assert "откроется через 40 мин" in v.note


# 6. closing edge: end is exclusive
def test_closing_edge() -> None:
    v = evaluate_hours("Mo-Su 10:00-22:00", at("Mo", 21, 59))
    assert v.status == "open"
    assert v.closes_in_min == 1
    assert "закроется через 1 мин" in v.note
    assert evaluate_hours("Mo-Su 10:00-22:00", at("Mo", 22)).status == "closed"


# 7. after closing -> opens tomorrow, no opens_in_min
def test_after_closing_opens_tomorrow() -> None:
    v = evaluate_hours("Mo-Su 10:00-22:00", at("Mo", 23, 30))
    assert (v.status, v.confidence) == ("closed", "high")
    assert v.opens_in_min is None
    assert "откроется завтра в 10:00" in v.note


# 8. explicit day off
def test_day_off() -> None:
    raw = "Tu-Su 10:00-20:00; Mo off"
    mon = evaluate_hours(raw, at("Mo", 12))
    assert (mon.status, mon.confidence) == ("closed", "high")
    assert "выходной" in mon.note
    tue = evaluate_hours(raw, at("Tu", 12))
    assert (tue.status, tue.confidence) == ("open", "high")


# 9. lunch break
def test_lunch_break() -> None:
    raw = "Mo-Fr 08:00-12:00,13:00-17:00"
    lunch = evaluate_hours(raw, at("We", 12, 30))
    assert (lunch.status, lunch.confidence) == ("closed", "high")
    assert lunch.opens_in_min == 30
    morning = evaluate_hours(raw, at("We", 10))
    assert morning.status == "open"
    assert morning.closes_in_min == 120


# 10. past midnight: Tuesday 01:00 is covered by Monday's rule
def test_past_midnight_previous_day_rule() -> None:
    v = evaluate_hours("Mo-Su 18:00-04:00", at("Tu", 1))
    assert (v.status, v.confidence) == ("open", "high")
    assert v.closes_in_min == 180


# 11. past midnight: evening of the same day
def test_past_midnight_same_evening() -> None:
    v = evaluate_hours("Mo-Su 18:00-04:00", at("Mo", 23, 30))
    assert v.status == "open"
    assert v.closes_in_min == 270


# 12. past midnight: morning gap
def test_past_midnight_morning_gap() -> None:
    v = evaluate_hours("Mo-Su 18:00-04:00", at("Mo", 5))
    assert (v.status, v.confidence) == ("closed", "high")
    assert v.opens_in_min == 780


# 13. weekday change: Friday night spills into Saturday; weekend itself not described
def test_weekday_change_after_midnight() -> None:
    raw = "Mo-Fr 18:00-02:00"
    sat = evaluate_hours(raw, at("Sa", 1))
    assert (sat.status, sat.confidence) == ("open", "high")
    assert sat.closes_in_min == 60
    sun = evaluate_hours(raw, at("Su", 1))
    assert (sun.status, sun.confidence) == ("closed", "medium")
    assert "по данным OSM сегодня не работает" in sun.note


# 14. day not described by any rule -> closed/medium
def test_undescribed_day_closed_medium() -> None:
    raw = "Mo-Fr 09:00-18:00; Sa 10:00-16:00"
    wed = evaluate_hours(raw, at("We", 12))
    assert (wed.status, wed.confidence) == ("open", "high")
    sun = evaluate_hours(raw, at("Su", 12))
    assert (sun.status, sun.confidence) == ("closed", "medium")
    assert sun.opens_in_min is None


# 15. list of days
def test_day_list() -> None:
    raw = "Mo,We,Fr 10:00-14:00"
    wed = evaluate_hours(raw, at("We", 11))
    assert (wed.status, wed.confidence) == ("open", "high")
    tue = evaluate_hours(raw, at("Tu", 11))
    assert (tue.status, tue.confidence) == ("closed", "medium")


# 16. wrap-around day range
def test_wrapping_day_range() -> None:
    raw = "Fr-Mo 12:00-20:00"
    sun = evaluate_hours(raw, at("Su", 13))
    assert (sun.status, sun.confidence) == ("open", "high")
    wed = evaluate_hours(raw, at("We", 13))
    assert (wed.status, wed.confidence) == ("closed", "medium")


# 17. later rule overrides earlier one for its days
def test_later_rule_overrides() -> None:
    raw = "Mo-Su 10:00-22:00; Su 12:00-18:00"
    sun = evaluate_hours(raw, at("Su", 11))
    assert (sun.status, sun.confidence) == ("closed", "high")
    assert sun.opens_in_min == 60
    assert evaluate_hours(raw, at("Su", 19)).status == "closed"
    mon = evaluate_hours(raw, at("Mo", 11))
    assert (mon.status, mon.confidence) == ("open", "high")


# 18. 24:00 as closing time
def test_until_midnight() -> None:
    v = evaluate_hours("Mo-Su 09:00-24:00", at("Mo", 23, 30))
    assert v.status == "open"
    assert v.closes_in_min == 30


# 19. formats we do not understand -> unknown/low
@pytest.mark.parametrize(
    "raw",
    [
        "sunrise-sunset",
        "by appointment",
        "Mo-Fr 10:00+",
        "Jan-Mar Mo-Fr 09:00-17:00",
        "Mo-Fr 09:00-18:00 || Sa 10:00-14:00",
        "Mo-Fr 25:00-26:00",
        "10:00-10:00",
        'Mo-Fr 09:00-18:00 "по записи"',
        "Mo[1] 10:00-12:00",
        "PH off",
    ],
)
def test_unparsable_is_unknown_low(raw: str) -> None:
    v = evaluate_hours(raw, at("Mo", 12))
    assert (v.status, v.confidence) == ("unknown", "low")
    assert v.closes_in_min is None and v.opens_in_min is None
    assert "проверьте" in v.note


# 20. timezone handling
def test_timezone_conversion() -> None:
    utc_morning = datetime(2026, 10, 19, 7, 0, tzinfo=UTC)  # 12:00 in Astana
    v = evaluate_hours("Mo-Su 10:00-13:00", utc_morning)
    assert v.status == "open"
    assert v.closes_in_min == 60
    naive = datetime(2026, 10, 19, 12, 0)  # treated as Astana local time
    assert evaluate_hours("Mo-Su 10:00-13:00", naive).closes_in_min == 60


def test_now_astana_is_utc_plus_5() -> None:
    assert now_astana().utcoffset() == timedelta(hours=5)


# 21. sloppy but unambiguous strings
@pytest.mark.parametrize(
    "raw", ["Mo-Su 10:00-22:00;", "  Mo-Su   10:00 - 22:00 ", "Mo-Su 10:00-22:00 ; ", "10:00-22:00"]
)
def test_tolerant_parsing(raw: str) -> None:
    v = evaluate_hours(raw, at("Mo", 12))
    assert (v.status, v.confidence) == ("open", "high")
    assert v.closes_in_min == 600


def test_single_digit_hour() -> None:
    v = evaluate_hours("Mo-Fr 9:00-18:00", at("Mo", 8, 30))
    assert v.status == "closed"
    assert v.opens_in_min == 30


# 22. always closed
def test_always_off() -> None:
    v = evaluate_hours("Mo-Su off", at("We", 12))
    assert (v.status, v.confidence) == ("closed", "high")


# 23. public / school holiday rules are ignored, not fatal
@pytest.mark.parametrize(
    "raw", ["Mo-Su 10:00-22:00; PH off", "Mo-Su 10:00-22:00; PH,SH 12:00-18:00"]
)
def test_holiday_rules_ignored(raw: str) -> None:
    v = evaluate_hours(raw, at("Mo", 12))
    assert (v.status, v.confidence) == ("open", "high")
    assert v.closes_in_min == 600
    assert "в праздники часы могут отличаться" in v.note


def test_holiday_inside_day_list() -> None:
    v = evaluate_hours("Mo-Fr,PH 10:00-18:00", at("Sa", 12))
    assert (v.status, v.confidence) == ("closed", "medium")


# 24. ", " before a weekday starts an additional rule
def test_additional_rule_after_comma() -> None:
    raw = "Mo-Sa 10:00-22:00, Su 11:00-21:00"
    sun_early = evaluate_hours(raw, at("Su", 10, 30))
    assert (sun_early.status, sun_early.confidence) == ("closed", "high")
    assert sun_early.opens_in_min == 30
    sun = evaluate_hours(raw, at("Su", 12))
    assert (sun.status, sun.confidence) == ("open", "high")
    assert sun.closes_in_min == 540
    sat = evaluate_hours(raw, at("Sa", 21))
    assert (sat.status, sat.closes_in_min) == ("open", 60)


def test_note_renders_schedule_in_russian() -> None:
    v = evaluate_hours("Mo-Fr 09:00-18:00; Sa 10:00-16:00", at("Mo", 12))
    assert "по данным OSM: Пн–Пт 09:00–18:00; Сб 10:00–16:00" in v.note


# real OSM strings (T10 fixture / T11 full dump): share we understand
REAL_FILES = [
    ROOT / "tests" / "fixtures" / "poi_sample.jsonl",
    ROOT / "data" / "processed" / "poi.jsonl",
]


@pytest.mark.parametrize("path", REAL_FILES, ids=["poi_sample", "poi_full"])
def test_real_opening_hours_parse_ratio(path: Path) -> None:
    if not path.exists():
        pytest.skip(f"{path.name} not produced yet (DATA lane)")
    raws = [row["opening_hours"] for row in read_jsonl(path) if row.get("opening_hours")]
    if not raws:
        pytest.skip("no opening_hours in file")
    low = [raw for raw in raws if evaluate_hours(raw, at("Tu", 15)).confidence == "low"]
    parsed = 1 - len(low) / len(raws)
    assert parsed >= 0.9, f"parsed {parsed:.0%}; not understood: {sorted(set(low))[:10]}"


# ------------------------------------------------------------ T73: kk / en notes

CYRILLIC = set("абвгдеёжзийклмнопрстуфхцчшщъыьэюя")


@pytest.mark.parametrize(
    ("raw", "moment"),
    [(None, at("Mo", 12)), ("Mo-Fr 09:00-18:00", at("Mo", 17, 30)),
     ("Mo-Fr 09:00-18:00", at("Mo", 8, 30)), ("Mo-Fr 09:00-18:00", at("Mo", 20)),
     ("Mo-Fr 09:00-18:00", at("Sa", 12)), ("24/7", at("We", 3)), ("Tu-Su 10:00-20:00; Mo off",
      at("Mo", 12)), ("sunrise-sunset", at("Mo", 12)), ("PH off", at("Mo", 12)),
     ("Mo-Su 10:00-22:00; PH off", at("Mo", 21, 30)), ("Mo-Sa 10:00-14:00", at("Mo", 9))],
)
def test_language_changes_only_the_wording(raw: str | None, moment: datetime) -> None:
    ru = evaluate_hours(raw, moment)
    for lang in ("kk", "en"):
        other = evaluate_hours(raw, moment, lang=lang)
        assert (other.status, other.confidence, other.closes_in_min, other.opens_in_min) == (
            ru.status, ru.confidence, ru.closes_in_min, ru.opens_in_min)
        assert other.note and other.note != ru.note
    assert not CYRILLIC & set(evaluate_hours(raw, moment, lang="en").note.lower())


def test_english_and_kazakh_notes_read_naturally() -> None:
    assert evaluate_hours("Mo-Fr 09:00-18:00", at("Mo", 17, 30), lang="en").note == (
        "closes in 30 min · per OSM: Mo–Fr 09:00–18:00")
    assert evaluate_hours("Mo-Fr 09:00-18:00", at("Mo", 8, 30), lang="en").note == (
        "closed, opens in 30 min · per OSM: Mo–Fr 09:00–18:00")
    assert evaluate_hours("24/7", at("We", 3), lang="en").note == "per OSM: daily 24/7"
    kk = evaluate_hours("Mo-Fr 09:00-18:00", at("Mo", 20), lang="kk").note
    assert kk == "жабық, ертең сағат 09:00-де ашылады · OSM деректері бойынша: Дс–Жм 09:00–18:00"
    assert evaluate_hours("Mo-Fr 09:00-18:00", at("Mo", 17, 30)).note == (
        "закроется через 30 мин · по данным OSM: Пн–Пт 09:00–18:00")  # ru is unchanged


def test_unknown_note_by_language() -> None:
    from app.hours import unknown_note
    from app.validate import UNKNOWN_HOURS_NOTE

    assert unknown_note() == UNKNOWN_HOURS_NOTE
    assert unknown_note("en") == "no opening-hours data, check before you go"
    assert "тексеріңіз" in unknown_note("kk")
