"""pipeline.normalize on tiny raw Overpass samples (no network)."""

from __future__ import annotations

from app.schemas import TAG_VOCAB
from pipeline.normalize import (
    PLAYGROUND_NAME,
    data_confidence,
    dedupe,
    hours_flags,
    landmark_pois,
    name_playgrounds,
    normalize_element,
    normalize_payload,
    normalize_payloads,
    osm_kind,
    popularity,
    raw_files,
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


def test_overlapping_chunks_take_each_element_once(tmp_path) -> None:
    chunk_a = {"qadam": {"fetched_at_unix": 1792314269},
               "elements": [node(30, {"amenity": "cafe", "name": "A"})]}
    chunk_b = {"qadam": {"fetched_at_unix": 1792400000},
               "elements": [node(30, {"amenity": "cafe", "name": "A"}),
                            node(31, {"amenity": "cafe", "name": "B"}, lat=51.2)]}
    pois = normalize_payloads([chunk_a, chunk_b])
    assert [p.id for p in pois] == ["osm:node/30", "osm:node/31"]
    assert pois[0].fetched_at == "2026-10-18"

    for name in ("osm_astana_tile_0.json", "osm_zone_x.json", "osm_fixture.json", "other.json"):
        (tmp_path / name).write_text("{}", encoding="utf-8")
    assert [p.name for p in raw_files("zones", tmp_path)] == ["osm_zone_x.json"]
    assert [p.name for p in raw_files("astana", tmp_path)] == [
        "osm_astana_tile_0.json", "osm_zone_x.json", "osm_fixture.json"]


LANDMARKS = [
    {"id": "palace_peace", "names": {"ru": "Пирамида", "kk": "Сарай", "en": "Pyramid"},
     "geo": {"lat": 51.12312, "lon": 71.46346}, "kind": "landmark", "verified": True},
    {"id": "khan_shatyr", "names": {"ru": "ТРЦ «Хан Шатыр»", "en": "Khan Shatyr"},
     "geo": {"lat": 51.13251, "lon": 71.40387}, "kind": "landmark", "verified": True},
    {"id": "nurly_zhol", "names": {"ru": "Вокзал"}, "geo": {"lat": 51.112, "lon": 71.529},
     "kind": "transport", "verified": True},
    {"id": "draft", "names": {"ru": "Черновик"}, "geo": {"lat": 51.1, "lon": 71.4},
     "kind": "landmark", "verified": False},
]
REFS = {"palace_peace": "way/166197368", "khan_shatyr": "way/460703779",
        "nurly_zhol": "way/1484667027", "draft": "way/1"}


def test_landmarks_become_pois_without_duplicates() -> None:
    khan = normalize_element(
        {"type": "way", "id": 460703779, "center": {"lat": 51.13251, "lon": 71.40387},
         "tags": {"shop": "mall", "name": "Хан Шатыр"}}, DATE)
    assert khan is not None
    extra = landmark_pois([khan], LANDMARKS, REFS, {"palace_peace": "Q2119741"}, DATE)
    assert [p.id for p in extra] == ["osm:way/166197368"]  # khan: present; transport, draft: no
    pyramid = extra[0]
    assert pyramid.category == "attraction" and pyramid.sources == ["osm", "manual"]
    assert pyramid.name == "Пирамида" and pyramid.name_i18n["en"] == "Pyramid"
    assert pyramid.description is None  # filled from Wikidata by pipeline.enrich
    assert pyramid.data_confidence == 0.5  # 0.3 + wikidata 0.1 + languages 0.1
    assert set(pyramid.tags) <= TAG_VOCAB

    # same name close by under another OSM id also counts as present
    other = khan.model_copy(update={"id": "osm:node/5", "name": "Хан Шатыр"})
    assert not [p for p in landmark_pois([other], LANDMARKS[1:2], REFS, {}, DATE)]


def test_playgrounds_named_after_nearest_poi_within_150_m() -> None:
    cafe = normalize_element(node(40, {"amenity": "cafe", "name": "Ласточка"}), DATE)
    near = normalize_element(node(41, {"leisure": "playground"}, lat=51.0894 + 0.0009), DATE)
    far = normalize_element(node(42, {"leisure": "playground"}, lat=51.0894 + 0.003), DATE)
    named = normalize_element(node(43, {"leisure": "playground", "name": "Мечта"},
                                   lat=51.0894 + 0.0005), DATE)
    assert cafe and near and far and named
    out = {p.id: p for p in name_playgrounds([cafe, near, far, named])}
    assert out["osm:node/41"].name == "Детская площадка у Ласточка"  # ~100 m
    assert out["osm:node/42"].name == "Детская площадка"  # ~330 m: nobody near
    assert out["osm:node/43"].name == "Мечта"
    assert out["osm:node/41"].data_confidence <= 0.4


def test_freshest_snapshot_wins_and_dates_the_record() -> None:
    stale = {"osm3s": {"timestamp_osm_base": "2026-06-01T08:52:28Z"},
             "qadam": {"fetched_at_unix": 1792316715},
             "elements": [node(50, {"amenity": "cafe", "name": "Старое имя"})]}
    fresh = {"osm3s": {"timestamp_osm_base": "2026-10-18T10:28:05Z"},
             "qadam": {"fetched_at_unix": 1792316000},
             "elements": [node(50, {"amenity": "cafe", "name": "Новое имя"})]}
    (poi,) = normalize_payloads([stale, fresh])
    assert poi.name == "Новое имя" and poi.fetched_at == "2026-10-18"
    (old,) = normalize_payloads([stale])
    assert old.fetched_at == "2026-06-01"  # snapshot date, not the day we downloaded it


def test_playground_anchor_needs_a_proper_name() -> None:
    from pipeline.normalize import has_proper_name

    assert not has_proper_name("Сквер") and not has_proper_name("Кафе 24")
    assert not has_proper_name("Парк") and not has_proper_name("саябақ")
    assert has_proper_name("Сквер Акжайык") and has_proper_name("Coffee Boom")
    assert has_proper_name("Бухарест саябағы") and has_proper_name("Golpas")

    square = normalize_element(node(60, {"leisure": "park", "name": "Сквер"}), DATE)
    cafe = normalize_element(node(61, {"amenity": "cafe", "name": "Ласточка"},
                                  lat=51.0894 + 0.0012), DATE)
    pg = normalize_element(node(62, {"leisure": "playground"}, lat=51.0894 + 0.0003), DATE)
    assert square and cafe and pg
    out = {p.id: p for p in name_playgrounds([square, cafe, pg])}
    # "Сквер" is 33 m away but generic; the cafe ~100 m away has a name of its own
    assert out["osm:node/62"].name == "Детская площадка у Ласточка"


def test_explicit_osm_indoor_tag() -> None:
    ride = normalize_element(
        node(80, {"tourism": "attraction", "name": "Динопарк", "indoor": "room"}), DATE)
    plain = normalize_element(node(81, {"tourism": "attraction", "name": "Колесо"}), DATE)
    assert ride is not None and ride.indoor is True and "indoor" in ride.tags
    assert plain is not None and plain.indoor is None


def test_inside_mall_footprint_marks_unknown_indoor_only() -> None:
    from pipeline.normalize import inside, mark_inside_malls

    square = [{"lat": 51.130, "lon": 71.400}, {"lat": 51.130, "lon": 71.410},
              {"lat": 51.135, "lon": 71.410}, {"lat": 51.135, "lon": 71.400},
              {"lat": 51.130, "lon": 71.400}]
    hole = [{"lat": 51.132, "lon": 71.404}, {"lat": 51.132, "lon": 71.406},
            {"lat": 51.133, "lon": 71.406}, {"lat": 51.133, "lon": 71.404},
            {"lat": 51.132, "lon": 71.404}]
    outlines = {"elements": [
        {"type": "relation", "id": 1, "members": [
            {"type": "way", "role": "outer", "geometry": square[:3]},  # outer split in two
            {"type": "way", "role": "outer", "geometry": square[2:]},
            {"type": "way", "role": "inner", "geometry": hole}]},
    ]}
    pairs = zip(square, square[1:], strict=False)
    edges = [((a["lat"], a["lon"]), (b["lat"], b["lon"])) for a, b in pairs]
    assert inside(51.131, 71.401, edges) and not inside(51.140, 71.401, edges)

    ride = normalize_element(node(90, {"tourism": "attraction", "name": "Ride"},
                                  lat=51.131, lon=71.401), DATE)
    in_hole = normalize_element(node(91, {"tourism": "attraction", "name": "Двор"},
                                     lat=51.1325, lon=71.405), DATE)
    outside = normalize_element(node(92, {"tourism": "attraction", "name": "Вне"},
                                     lat=51.140, lon=71.401), DATE)
    park = normalize_element(node(93, {"leisure": "park", "name": "Сквер Х"},
                                  lat=51.131, lon=71.402), DATE)
    assert ride and in_hole and outside and park
    out, marked = mark_inside_malls([ride, in_hole, outside, park], outlines)
    by_id = {p.id: p for p in out}
    assert marked == 1 and by_id["osm:node/90"].indoor is True
    assert "indoor" in by_id["osm:node/90"].tags
    assert by_id["osm:node/91"].indoor is None  # courtyard hole: not inside
    assert by_id["osm:node/92"].indoor is None
    assert by_id["osm:node/93"].indoor is False  # a mall *area*: known values stay
    assert mark_inside_malls([ride], None) == ([ride], 0)

    # the same footprint as a building: the playground/park under the roof is indoor
    outlines["elements"][0]["tags"] = {"shop": "mall", "building": "retail"}
    out, marked = mark_inside_malls([park], outlines)
    assert marked == 1 and out[0].indoor is True and "outdoor" not in out[0].tags


def test_element_only_in_an_older_snapshot_is_dropped_where_fresh_data_covers_it() -> None:
    fresh = {"osm3s": {"timestamp_osm_base": "2026-10-18T10:00:00Z"},
             "qadam": {"filters": ["51.03,71.35,51.11,71.5"]},
             "elements": [{"type": "way", "id": 1460763574,
                           "center": {"lat": 51.09345, "lon": 71.41756},
                           "tags": {"tourism": "attraction",
                                    "name": "25-ти летие Независимости"}}]}
    june = {"osm3s": {"timestamp_osm_base": "2026-06-01T08:00:00Z"},
            "qadam": {"filters": ["around:2000,51.08925,71.41619"]},
            "elements": [
                node(13041707198, {"tourism": "attraction", "name": "25-ти летие Независемости"},
                     lat=51.09345, lon=71.41756),  # typo, deleted since June
                node(7, {"amenity": "cafe", "name": "Вне свежего куска"}, lat=51.12, lon=71.40),
            ]}
    stats: dict[str, int] = {}
    pois = normalize_payloads([june, fresh], stats)
    names = sorted(p.name for p in pois)
    assert names == ["25-ти летие Независимости", "Вне свежего куска"]
    assert stats == {"stale": 1}
