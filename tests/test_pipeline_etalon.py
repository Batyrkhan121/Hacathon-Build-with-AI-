"""pipeline.etalon on an in-memory CSV and tiny POI list."""

from __future__ import annotations

from pathlib import Path

from app.schemas import POI
from pipeline.etalon import compare, headline, hours_status, parse_schedule, read_rows


def poi(i: int, name: str, hours: str | None, lat: float = 51.13, lon: float = 71.40) -> POI:
    return POI.model_validate({
        "id": f"osm:node/{i}", "name": name, "category": "cafe",
        "geo": {"lat": lat, "lon": lon}, "opening_hours": hours,
        "sources": ["osm"], "data_confidence": 0.5, "fetched_at": "2026-10-18",
    })


def test_schedules_compare_by_meaning() -> None:
    assert parse_schedule("Mo-Su 10:00-22:00") == parse_schedule("ежедневно 10:00–22:00")
    assert parse_schedule("Пн-Пт 09:00-18:00; Сб 10:00-16:00") == parse_schedule(
        "Mo-Fr 09:00-18:00; Sa 10:00-16:00")
    assert parse_schedule("24/7") == parse_schedule("круглосуточно")
    assert parse_schedule("Mo-Su 10:00-22:00") != parse_schedule("Mo-Su 10:00-23:00")
    assert parse_schedule("Mo-Fr 10:00-22:00; PH off") is None
    assert hours_status("10:00-22:00", "Mo-Su 10:00-22:00") == "match"
    assert hours_status("10:00-22:00", None) == "no_ours"
    assert hours_status("", "Mo-Su 10:00-22:00") == "no_etalon"


def test_read_and_compare(tmp_path: Path) -> None:
    csv_path = tmp_path / "etalon.csv"
    csv_path.write_text(
        "Название;Часы работы;Широта;Долгота\n"
        "Хан Шатыр;ежедневно 10:00-23:00;51.1325;71.4039\n"
        "Кофейня Ласточка;Mo-Fr 08:00-20:00;51.13;71.40\n"
        "Starbucks;Mo-Su 08:00-22:00;51.20;71.50\n"
        "Несуществующее место;10:00-18:00;;\n",
        encoding="utf-8-sig",
    )
    rows = read_rows(csv_path)
    assert rows[0] == {"name": "Хан Шатыр", "hours": "ежедневно 10:00-23:00",
                       "lat": "51.1325", "lon": "71.4039"}
    pois = [
        poi(1, "ТРЦ Хан Шатыр", "Mo-Su 10:00-23:00", 51.13251, 71.40387),
        poi(2, "Ласточка", "Mo-Fr 08:00-21:00"),
        poi(3, "Starbucks", "Mo-Su 08:00-22:00", 51.0894, 71.4161),  # far from the row
    ]
    results = compare(rows, pois)
    assert [r.hours_status for r in results] == ["match", "mismatch", "not_found", "not_found"]
    text = headline(results)
    assert "найдено в данных OSM 2 из 4" in text and "часы совпали у 1 из 2" in text


def test_to_osm_hours_round_trip() -> None:
    from pipeline.etalon import to_osm_hours

    sched = parse_schedule("Пн-Пт 09:00-18:00; Сб 10:00-16:00")
    assert sched is not None and to_osm_hours(sched) == "Mo-Fr 09:00-18:00; Sa 10:00-16:00"
    every = parse_schedule("ежедневно 10:00–00:00")
    assert every is not None and to_osm_hours(every) == "Mo-Su 10:00-24:00"
    clock = parse_schedule("круглосуточно")
    assert clock is not None and to_osm_hours(clock) == "24/7"


def test_missing_places_become_manual_pois_once() -> None:
    from pipeline.etalon import apply_etalon, render

    rows = [
        {"name": "Хан Шатыр", "hours": "ежедневно 10:00-23:00", "lat": "51.1325",
         "lon": "71.4039", "source": "https://khanshatyr.com"},
        {"name": "Музей Нур Алем", "hours": "Вт-Вс 10:00-19:00", "lat": "51.0893",
         "lon": "71.4162", "address": "пр. Мангилик Ел, 53/1", "category": "Музей",
         "source": "https://example.kz/museum"},
        {"name": "Без координат", "hours": "10:00-18:00", "source": "https://x.kz"},
        {"name": "Без ссылки", "lat": "51.1", "lon": "71.4"},
    ]
    osm = [poi(1, "ТРЦ Хан Шатыр", "Mo-Su 10:00-23:00", 51.13251, 71.40387)]
    out, outcome = apply_etalon(osm, rows, "2026-10-18")
    assert [p.id for p in out] == ["manual:poi/2", "osm:node/1"]
    museum = out[0]
    assert museum.sources == ["manual"] and museum.category == "museum"
    assert museum.website == "https://example.kz/museum"
    assert museum.opening_hours == "Tu-Su 10:00-19:00"  # from the row, OSM syntax
    assert museum.address == "пр. Мангилик Ел, 53/1" and museum.indoor is True
    assert [why for _, why in outcome.skipped] == ["нет координат",
                                                   "нет ссылки на официальный источник"]
    assert "найдено в данных OSM 1 из 4" in headline(outcome.results)
    assert "Добавлено вручную: 1" in render(outcome)

    # rerun on the output: the manual POI is not counted as found, nor added twice
    out2, outcome2 = apply_etalon(out, rows, "2026-10-18")
    assert [p.id for p in out2] == [p.id for p in out]
    assert "найдено в данных OSM 1 из 4" in headline(outcome2.results)


def test_unknown_etalon_category_is_reported() -> None:
    from pipeline.etalon import apply_etalon, render

    rows = [{"name": "Новый музей", "lat": "51.1", "lon": "71.4", "category": "Музееей",
             "source": "https://example.kz"}]
    out, outcome = apply_etalon([], rows, "2026-10-18")
    assert out[0].category == "other"
    assert outcome.unknown_categories == [("Новый музей", "Музееей")]
    assert "Категория не распознана" in render(outcome)
