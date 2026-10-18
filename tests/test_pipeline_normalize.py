"""pipeline.normalize on tiny raw Overpass samples (no network)."""

from __future__ import annotations

from app.schemas import TAG_VOCAB
from pipeline.normalize import (
    PLAYGROUND_NAME,
    data_confidence,
    dedupe,
    hours_flags,
    normalize_element,
    normalize_payload,
    osm_kind,
    popularity,
)

DATE = "2026-10-18"


def node(id_: int, tags: dict[str, str], lat: float = 51.0894, lon: float = 71.4161) -> dict:
    return {"type": "node", "id": id_, "lat": lat, "lon": lon, "tags": tags}


def test_cafe_basic_fields() -> None:
    poi = normalize_element(
        node(
            1,
            {
                "amenity": "cafe",
                "name": "Coffee Point",
                "name:ru": "Кофе Поинт",
                "name:en": "Coffee Point",
                "internet_access": "wlan",
                "opening_hours": "Mo-Su 08:00-23:30",
                "addr:street": "проспект Кабанбай батыра",
                "addr:housenumber": "62",
                "wheelchair": "limited",
            },
        ),
        DATE,
    )
    assert poi is not None
    assert poi.id == "osm:node/1"
    assert poi.name == "Кофе Поинт"
    assert poi.name_i18n == {"ru": "Кофе Поинт", "en": "Coffee Point"}
    assert poi.category == "cafe"
    assert set(poi.tags) == {"coffee", "wifi", "late_open", "indoor"}
    assert poi.address == "проспект Кабанбай батыра, 62"
    assert poi.wheelchair == "limited"
    assert poi.indoor is True
    assert poi.h3_r9 and len(poi.h3_r9) == 15
    assert poi.sources == ["osm"]
    assert poi.rating is None
    assert poi.fetched_at == DATE
    # 0.3 + hours 0.15 + address 0.1 + two languages 0.1
    assert poi.data_confidence == 0.65


def test_way_uses_center_and_unnamed_is_dropped() -> None:
    way = {
        "type": "way",
        "id": 7,
        "center": {"lat": 51.1, "lon": 71.4},
        "tags": {"leisure": "park", "name": "Парк"},
    }
    poi = normalize_element(way, DATE)
    assert poi is not None and poi.id == "osm:way/7"
    assert (poi.geo.lat, poi.geo.lon) == (51.1, 71.4)
    assert set(poi.tags) == {"outdoor", "walk"} and poi.indoor is False
    assert normalize_element(node(2, {"amenity": "cafe"}), DATE) is None


def test_unnamed_playground_gets_name_and_low_confidence() -> None:
    poi = normalize_element(
        node(3, {"leisure": "playground", "opening_hours": "24/7", "wikidata": "Q1",
                 "addr:street": "x", "website": "https://x"}),
        DATE,
    )
    assert poi is not None
    assert poi.name == PLAYGROUND_NAME
    assert poi.category == "playground"
    assert poi.kids_friendly is True and "kids" in poi.tags
    assert poi.data_confidence <= 0.4


def test_category_priority() -> None:
    assert osm_kind({"tourism": "museum", "amenity": "arts_centre"}) == ("museum", "museum")
    assert osm_kind({"tourism": "attraction", "leisure": "park"}) == ("park", "park")
    assert osm_kind({"historic": "monument", "tourism": "attraction"}) == ("monument", "historic")
    assert osm_kind({"tourism": "artwork"}) == ("monument", "artwork")
    assert osm_kind({"amenity": "coworking_space"}) == ("other", "coworking_space")
    assert osm_kind({"amenity": "parking"}) is None


def test_tags_only_from_data_and_inside_vocab() -> None:
    cowork = normalize_element(node(4, {"amenity": "coworking_space", "name": "Hub"}), DATE)
    assert cowork is not None and {"work_friendly", "wifi"} <= set(cowork.tags)
    plain = normalize_element(node(5, {"amenity": "restaurant", "name": "R"}), DATE)
    assert plain is not None
    assert "wifi" not in plain.tags and "power_outlets" not in plain.tags
    assert "vegetarian" not in plain.tags and "wheelchair" not in plain.tags
    rich = normalize_element(
        node(6, {"amenity": "restaurant", "name": "Q", "cuisine": "kazakh;regional",
                 "diet:vegetarian": "yes", "wheelchair": "yes", "fee": "no",
                 "live_music": "yes", "power_supply": "yes"}),
        DATE,
    )
    assert rich is not None
    assert {"local_cuisine", "vegetarian", "wheelchair", "free", "live_music",
            "power_outlets", "food"} <= set(rich.tags)
    assert set(rich.tags) <= TAG_VOCAB


def test_hours_flags() -> None:
    assert hours_flags("24/7") == {"24_7", "late_open"}
    assert hours_flags("Mo-Su 10:00-02:00") == {"late_open"}
    assert hours_flags("Mo-Th 10:00-22:00; Fr-Sa 10:00-24:00") == {"late_open"}
    assert hours_flags("Mo-Su 08:00-22:00") == set()
    assert hours_flags(None) == set()


def test_confidence_and_popularity_formulas() -> None:
    full = data_confidence(has_hours=True, has_contact=True, has_description=True,
                           has_wikidata=True, has_address=True, multilingual=True)
    assert full == 1.0
    empty = data_confidence(has_hours=False, has_contact=False, has_description=False,
                            has_wikidata=False, has_address=False, multilingual=False)
    assert empty == 0.3
    assert popularity({"name": "x"}, "cafe") == 0.01
    assert popularity({"wikidata": "Q1", "brand": "B"}, "museum") == 0.72
    many = {f"k{i}": "v" for i in range(60)} | {"wikidata": "Q"}
    assert popularity(many, "museum") == 0.9


def test_dedupe_same_name_close_keeps_richer() -> None:
    payload = {
        "qadam": {"fetched_at_unix": 1792314269},
        "elements": [
            node(10, {"shop": "mall", "name": "Мега Силк Вей", "name:en": "Mega SilkWay Astana"}),
            {"type": "way", "id": 11, "center": {"lat": 51.08945, "lon": 71.41605},
             "tags": {"shop": "mall", "name": "Mega Silk Way",
                      "opening_hours": "Mo-Su 10:00-22:00"}},
            # same name, 1.5 km away: a separate branch, kept
            node(12, {"amenity": "bar", "name": "Golpas"}, lat=51.10),
            node(13, {"amenity": "bar", "name": "Golpas"}),
        ],
    }
    pois = normalize_payload(payload)
    ids = [p.id for p in pois]
    assert "osm:way/11" in ids and "osm:node/10" not in ids
    assert {"osm:node/12", "osm:node/13"} <= set(ids)
    assert all(p.fetched_at == "2026-10-18" for p in pois)


def test_dedupe_short_names_need_exact_match() -> None:
    a = normalize_element(node(20, {"amenity": "cafe", "name": "Coco"}), DATE)
    b = normalize_element(node(21, {"amenity": "cafe", "name": "Cocoa"}), DATE)
    assert a is not None and b is not None
    assert len(dedupe([a, b])) == 2
