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
        # Baiterek: a landmark found by its own OSM tags that also got an override
        poi("osm:way/230401645", ["osm", "manual"]),
    ]
    raw = PAYLOAD | {"elements": [*PAYLOAD["elements"], {
        "type": "way", "id": 230401645, "center": {"lat": 51.128, "lon": 71.43},
        "tags": {"historic": "monument", "name": "Бәйтерек"}}]}
    text = build_report(pois, [raw], LANDMARKS)
    assert "Ориентиры, добавленные как места (объект OSM + ручная разметка): 1." in text


def test_report_walk_graph_section_states_partial_coverage() -> None:
    walk = {
        "osm_snapshot": "2026-05-31", "osm_snapshots": ["2026-05-31", "2026-10-18"],
        "created_at": "2026-10-19T01:30:00+05:00",
        "nodes": 1200, "edges": 1500, "total_length_km": 310.5,
        "largest_component_share": 0.9712, "crossings": 80, "unmarked_crossings": 12,
        "signals": 20, "kerbs_lowered": 9, "crossings_from_way_tags": 6,
        "crossing_nodes_added": 2, "steps_edges": 4, "lit_edges": 700,
        "crossing_edges": 60, "poi_total": 100, "poi_snapped": 40, "poi_snapped_share": 0.4,
        "poi_snap_median_m": 18.0, "landmarks_total": 9, "landmarks_snapped": 7,
        "snap_max_m": 150, "file_bytes": 2_500_000, "license": "ODbL 1.0",
        "nodes_outside_bbox": 3, "nodes_bbox": [51.0, 71.1, 51.3, 71.7],
        "coverage": "partial", "tiles_total": 9,
        "tiles_loaded": ["walk_tile_1", "walk_tile_4"], "tiles_missing": ["walk_tile_0"],
        "tiles_partial": [], "poi_in_coverage": 45, "poi_snapped_share_in_coverage": 0.889,
    }
    pois = normalize_payload(PAYLOAD)
    text = build_report(pois, [PAYLOAD], LANDMARKS, walk_meta=walk)
    section = text.split("## Пешеходный граф", 1)[1]
    for piece in ("Узлов: 1200; рёбер: 1500", "97.1% узлов", "Переходов: 80",
                  "footway=crossing у 6 узлов (из них 2 новых", "светофоров: 20",
                  "лестницами: 4", "40 из 100 (40.0%)", "медиана 18 м", "ориентиров: 7 из 9",
                  "2.5 МБ; лицензия: ODbL 1.0", "за границей bbox: 3",
                  "разных снимков: 2026-05-31, 2026-10-18",
                  "Покрытие частичное: 2 из 9 плиток", "не скачаны: walk_tile_0",
                  "(45) привязано 88.9%"):
        assert piece in section, piece
    assert "## Пешеходный граф" not in build_report(pois, [PAYLOAD], LANDMARKS)
    full = build_report(pois, [PAYLOAD], LANDMARKS,
                        walk_meta={**walk, "coverage": "full", "tiles_loaded": ["t"] * 9})
    assert "Покрытие: весь город, 9 из 9 плиток." in full
