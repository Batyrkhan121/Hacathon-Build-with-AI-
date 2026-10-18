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
    assert "найдено в наших данных 2 из 4" in text and "часы совпали у 1 из 2" in text
