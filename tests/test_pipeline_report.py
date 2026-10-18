"""pipeline.report on a tiny in-memory dataset (no network, no files)."""

from __future__ import annotations

from datetime import datetime

from app.hours import ASTANA_TZ
from pipeline.normalize import normalize_payload
from pipeline.report import build_report

PAYLOAD = {
    "qadam": {"fetched_at_unix": 1792314269, "chunk": "zone_test", "endpoint": "https://m/i"},
    "elements": [
        {"type": "node", "id": 1, "lat": 51.1283, "lon": 71.4305,
         "tags": {"amenity": "cafe", "name": "Кофе", "opening_hours": "Mo-Su 08:00-22:00"}},
        {"type": "node", "id": 2, "lat": 51.1290, "lon": 71.4310,
         "tags": {"tourism": "museum", "name": "Музей", "wikidata": "Q1"}},
        {"type": "node", "id": 3, "lat": 51.2500, "lon": 71.6000,
         "tags": {"leisure": "park", "name": "Окраина"}},
    ],
}
LANDMARKS = [{"id": "baiterek", "names": {"ru": "Байтерек"},
              "geo": {"lat": 51.12829, "lon": 71.43045}}]


def test_report_sections_and_numbers() -> None:
    pois = normalize_payload(PAYLOAD)
    text = build_report(pois, [PAYLOAD], LANDMARKS, missing_chunks=["astana_tile_8"],
                        generated_at=datetime(2026, 10, 18, 15, 0, tzinfo=ASTANA_TZ))
    assert text.startswith("# Данные Qadam")
    assert "после нормализации и дедупликации: 3 POI" in text
    assert "| cafe | 1 | 100% |" in text
    assert "тег `wikidata` в OSM: 33% (1)" in text
    assert "Не докачано (1):** astana_tile_8" in text
    assert "| Байтерек | 2 |" in text
    assert "https://m/i (1)" in text
    assert "```json" in text and '"id": "osm:node/' in text


def test_report_zones_coverage_and_computed_conclusion() -> None:
    pois = normalize_payload(PAYLOAD)
    text = build_report(pois, [PAYLOAD], LANDMARKS)
    assert "без плиток всего города" in text and "Все плитки" not in text
    assert "больше всего данных рядом с «Байтерек» (2 POI" in text
    assert "Часы работы известны у 33% мест" in text


def test_report_warns_about_partial_chunks() -> None:
    cut = PAYLOAD | {"remark": "runtime error: Query timed out"}
    text = build_report(normalize_payload(cut), [cut], LANDMARKS)
    assert "Неполные ответы Overpass (remark): zone_test" in text


def test_report_states_age_of_oldest_chunk_and_mall_outlines() -> None:
    old = PAYLOAD | {"qadam": PAYLOAD["qadam"] | {"chunk": "zone_old",
                                                   "fetched_at_unix": 1792314269 - 3 * 86400}}
    when = datetime.fromtimestamp(1792314269 + 2 * 3600, ASTANA_TZ)
    text = build_report(normalize_payload(PAYLOAD), [PAYLOAD, old], LANDMARKS,
                        generated_at=when, outlines_meta={"fetched_at_unix": 1792314269})
    assert "самый старый кусок zone_old" in text and "(74 ч назад)" in text
    assert "Контуры ТРЦ: скачаны" in text and "(2 ч назад)" in text
    none = build_report(normalize_payload(PAYLOAD), [PAYLOAD], LANDMARKS, generated_at=when)
    assert "Контуры ТРЦ: не скачаны" in none


def test_landmark_count_ignores_plain_overrides() -> None:
    from app.schemas import POI

    def poi(pid: str, sources: list[str]) -> POI:
        return POI.model_validate({"id": pid, "name": pid, "category": "attraction",
                                   "geo": {"lat": 51.1, "lon": 71.4}, "sources": sources,
                                   "data_confidence": 0.5, "fetched_at": "2026-10-18"})

    pois = [
        poi("osm:way/166197368", ["osm", "manual"]),  # the Pyramid: a landmark added as POI
        poi("osm:node/5126825313", ["osm", "manual"]),  # the circus: only an override
        # Baiterek: a landmark found by its own OSM tags that also got an override
        poi("osm:way/230401645", ["osm", "manual"]),
    ]
    raw = PAYLOAD | {"elements": [*PAYLOAD["elements"], {
        "type": "way", "id": 230401645, "center": {"lat": 51.128, "lon": 71.43},
        "tags": {"historic": "monument", "name": "Бәйтерек"}}]}
    text = build_report(pois, [raw], LANDMARKS)
    assert "Ориентиры, добавленные как места (объект OSM + ручная разметка): 1." in text
