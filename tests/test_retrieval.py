"""T22: retrieval — hard filters, scoring, MMR diversity, deterministic reasons."""

from __future__ import annotations

from datetime import datetime

import numpy as np
import pytest

from app.geo import haversine_m, to_h3, walk_minutes
from app.hours import ASTANA_TZ
from app.retrieval import WEIGHTS, explain, lexical_relevance, retrieve
from app.schemas import POI, GeoPoint, Landmark, QueryIntent, SearchContext, WeatherInfo
from app.store import Store, load_pois
from app.validate import template_cards

from .conftest import EXPO, FIXTURES

ORIGIN = GeoPoint(lat=EXPO[0], lon=EXPO[1])
TUE_NOON = datetime(2026, 10, 20, 12, 0, tzinfo=ASTANA_TZ)
MON_NOON = datetime(2026, 10, 19, 12, 0, tzinfo=ASTANA_TZ)


def make_store(pois: list[POI], embeddings: np.ndarray | None = None) -> Store:
    return Store(pois=pois, by_id={p.id: p for p in pois}, landmarks={}, embeddings=embeddings)


def make_ctx(
    at: datetime = TUE_NOON, radius: int = 1200, weather: WeatherInfo | None = None
) -> SearchContext:
    return SearchContext(
        origin=ORIGIN,
        h3_cell=to_h3(ORIGIN),
        h3_res=9,
        area_label="у EXPO",
        local_time=at,
        weather=weather,
        radius_m=radius,
    )


def weather(outdoor_ok: bool) -> WeatherInfo:
    return WeatherInfo(
        temp_c=12 if outdoor_ok else -15,
        summary="тест",
        outdoor_ok=outdoor_ok,
        fetched_at=TUE_NOON,
    )


def intent(query: str = "куда сходить", **kwargs: object) -> QueryIntent:
    return QueryIntent(raw_query=query, **kwargs)


def ids(cands: list) -> list[str]:
    return [c.poi.id for c in cands]


def make_poi(poi_id: str, category: str, dlat: float = 0.001, **kwargs: object) -> POI:
    data: dict[str, object] = {
        "id": f"test:poi/{poi_id}",
        "name": kwargs.pop("name", f"Место {poi_id}"),
        "category": category,
        "geo": {"lat": EXPO[0] + dlat, "lon": EXPO[1]},
        "sources": ["osm"],
        "data_confidence": 0.6,
        "fetched_at": "2026-10-18",
    }
    data.update(kwargs)
    return POI.model_validate(data)


# ------------------------------------------------------------------ filters


def test_radius_filter(synthetic_pois: list[POI]) -> None:
    out = retrieve(make_store(synthetic_pois), intent(), make_ctx(), None, k=20)
    assert out
    assert all(c.distance_m <= 1200 for c in out)
    assert {"test:poi/10", "test:poi/11", "test:poi/14"}.isdisjoint(ids(out))


def test_distance_and_walk_come_from_code(synthetic_pois: list[POI]) -> None:
    out = retrieve(make_store(synthetic_pois), intent(), make_ctx(), None, k=20)
    museum = next(c for c in out if c.poi.id == "test:poi/3")
    assert 100 < museum.distance_m < 200
    assert museum.walk_min == walk_minutes(museum.distance_m)


def test_categories_filter(synthetic_pois: list[POI]) -> None:
    out = retrieve(
        make_store(synthetic_pois), intent(categories=["museum"]), make_ctx(), None, k=20
    )
    assert ids(out) == ["test:poi/3"]


def test_avoid_tags_filter(synthetic_pois: list[POI]) -> None:
    out = retrieve(
        make_store(synthetic_pois), intent(avoid_tags=["outdoor"]), make_ctx(), None, k=20
    )
    assert out
    assert all("outdoor" not in c.poi.tags for c in out)


def test_with_kids_filter(synthetic_pois: list[POI]) -> None:
    out = retrieve(make_store(synthetic_pois), intent(with_kids=True), make_ctx(), None, k=20)
    assert set(ids(out)) == {"test:poi/3", "test:poi/4", "test:poi/5", "test:poi/7"}
    assert all("подходит детям" in c.matched for c in out)


def test_wheelchair_filter_and_penalty() -> None:
    pois = [
        make_poi("1", "cafe", wheelchair="yes"),
        make_poi("2", "cafe", wheelchair="unknown"),
        make_poi("3", "cafe", wheelchair="no"),
    ]
    out = retrieve(make_store(pois), intent(wheelchair=True), make_ctx(), None, k=5)
    assert ids(out) == ["test:poi/1", "test:poi/2"]
    unknown = out[1]
    assert "доступность не подтверждена" in unknown.matched
    assert unknown.score_parts["penalty"] < 0
    assert "доступно для коляски" in out[0].matched


def test_indoor_filter(synthetic_pois: list[POI]) -> None:
    out = retrieve(make_store(synthetic_pois), intent(indoor="indoor"), make_ctx(), None, k=20)
    assert out
    assert {c.poi.category for c in out}.isdisjoint({"park", "playground", "viewpoint", "monument"})


def test_outdoor_filter(synthetic_pois: list[POI]) -> None:
    out = retrieve(make_store(synthetic_pois), intent(indoor="outdoor"), make_ctx(), None, k=20)
    assert out
    assert all(c.poi.indoor is not True for c in out)


def test_closed_places_dropped(synthetic_pois: list[POI]) -> None:
    store = make_store(synthetic_pois)
    monday = retrieve(store, intent(), make_ctx(at=MON_NOON, radius=1500), None, k=20)
    assert "test:poi/3" not in ids(monday)  # "Mo off" -> closed/high
    assert "test:poi/10" not in ids(monday)  # Monday not described -> closed/medium
    tuesday = retrieve(store, intent(), make_ctx(radius=1500), None, k=20)
    assert {"test:poi/3", "test:poi/10"} <= set(ids(tuesday))


def test_closing_soon_dropped(synthetic_pois: list[POI]) -> None:
    late = datetime(2026, 10, 20, 21, 45, tzinfo=ASTANA_TZ)
    out = retrieve(make_store(synthetic_pois), intent(), make_ctx(at=late), None, k=20)
    assert "test:poi/1" not in ids(out)  # closes at 22:00
    assert "test:poi/7" not in ids(out)
    assert "test:poi/9" in ids(out)  # open until 23:00


def test_night_mode(synthetic_pois: list[POI]) -> None:
    night = datetime(2026, 10, 20, 1, 0, tzinfo=ASTANA_TZ)
    out = retrieve(make_store(synthetic_pois), intent(night=True), make_ctx(at=night), None, k=20)
    assert set(ids(out)) == {"test:poi/2", "test:poi/6", "test:poi/8"}


def test_unknown_hours_kept_but_penalised(synthetic_pois: list[POI]) -> None:
    out = retrieve(make_store(synthetic_pois), intent(), make_ctx(), None, k=20)
    park = next(c for c in out if c.poi.id == "test:poi/4")
    assert park.hours.status == "unknown"
    assert park.score_parts["open"] == 0.5
    assert "часы работы не указаны" in park.matched


# ------------------------------------------------------------------ scoring


def test_lexical_query_finds_quiet_cafe(synthetic_pois: list[POI]) -> None:
    q = intent(
        "Хочу тихое место с кофе и розеткой недалеко",
        must_tags=["coffee", "power_outlets", "quiet"],
    )
    out = retrieve(make_store(synthetic_pois), q, make_ctx(), None, k=8)
    assert out[0].poi.id == "test:poi/1"
    assert "есть кофе (данные команды)" in out[0].matched
    assert "есть розетки (данные команды)" in out[0].matched


def test_lexical_query_without_tags(synthetic_pois: list[POI]) -> None:
    out = retrieve(
        make_store(synthetic_pois),
        intent("где рядом детские площадки"),
        make_ctx(),
        None,
        k=8,
    )
    # keyword match is the strongest for the playground; final order also weighs popularity
    # and confirmed hours (starting WEIGHTS, tuned by eval in T70)
    best = max(out, key=lambda c: c.score_parts["relevance"])
    assert best.poi.id == "test:poi/5"
    assert "test:poi/5" in ids(out[:3])


def test_semantic_relevance(synthetic_pois: list[POI]) -> None:
    matrix = np.zeros((len(synthetic_pois), 4), dtype=np.float32)
    matrix[:, 1] = 1.0
    target = next(i for i, p in enumerate(synthetic_pois) if p.id == "test:poi/13")
    matrix[target] = [1.0, 0.0, 0.0, 0.0]
    store = make_store(synthetic_pois, embeddings=matrix)
    query_vec = np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32)
    out = retrieve(store, intent("что-нибудь историческое"), make_ctx(), query_vec, k=5)
    assert out[0].poi.id == "test:poi/13"
    assert out[0].score_parts["relevance"] > out[1].score_parts["relevance"]


def test_semantic_ignored_on_dim_mismatch(synthetic_pois: list[POI]) -> None:
    matrix = np.ones((len(synthetic_pois), 4), dtype=np.float32) / 2
    store = make_store(synthetic_pois, embeddings=matrix)
    out = retrieve(store, intent(), make_ctx(), np.ones(3, dtype=np.float32), k=5)
    assert len(out) == 5


def test_score_is_weighted_sum(synthetic_pois: list[POI]) -> None:
    out = retrieve(make_store(synthetic_pois), intent("кофе"), make_ctx(), None, k=8)
    for c in out:
        parts = c.score_parts
        expected = sum(WEIGHTS[k] * parts[k] for k in WEIGHTS) + parts.get("penalty", 0.0)
        assert c.score == pytest.approx(expected, abs=1e-3)
        assert 0.0 <= parts["proximity"] <= 1.0
        assert 0.0 <= parts["relevance"] <= 1.0


def test_weather_fit(synthetic_pois: list[POI]) -> None:
    store = make_store(synthetic_pois)

    def parts(w: WeatherInfo | None) -> dict[str, float]:
        out = retrieve(store, intent(), make_ctx(weather=w), None, k=20)
        return {c.poi.id: c.score_parts["weather"] for c in out}

    bad, good, none = parts(weather(False)), parts(weather(True)), parts(None)
    assert (bad["test:poi/7"], bad["test:poi/4"]) == (1.0, 0.2)  # mall vs park
    assert (good["test:poi/7"], good["test:poi/4"]) == (0.7, 1.0)
    assert set(none.values()) == {0.7}


def test_bad_weather_prefers_indoor(synthetic_pois: list[POI]) -> None:
    out = retrieve(
        make_store(synthetic_pois), intent(), make_ctx(weather=weather(False)), None, k=8
    )
    assert out[0].poi.indoor is True
    assert any("укрытие от погоды" in m for m in out[0].matched)


# -------------------------------------------------------------- diversity


def test_mmr_caps_same_category_in_top3() -> None:
    pois = [
        make_poi("1", "cafe", 0.0010, tags=["coffee"]),
        make_poi("2", "cafe", 0.0011, tags=["coffee"]),
        make_poi("3", "cafe", 0.0012, tags=["coffee"]),
        make_poi("4", "museum", 0.0013, tags=["coffee"]),  # museum with a coffee point
    ]
    out = retrieve(make_store(pois), intent("кофе"), make_ctx(), None, k=4)
    assert [c.poi.category for c in out[:3]].count("cafe") == 2
    assert "test:poi/4" in ids(out[:3])


def test_mmr_keeps_relevance_when_alternative_is_weak() -> None:
    pois = [
        make_poi("1", "cafe", 0.0010, tags=["coffee", "quiet", "power_outlets"]),
        make_poi("2", "cafe", 0.0011, tags=["coffee", "quiet", "power_outlets"]),
        make_poi("3", "cafe", 0.0012, tags=["coffee", "quiet", "power_outlets"]),
        make_poi("4", "monument", 0.0100, data_confidence=0.1),
    ]
    q = intent("тихое кафе с кофе и розеткой", must_tags=["coffee", "quiet", "power_outlets"])
    out = retrieve(make_store(pois), q, make_ctx(), None, k=4)
    assert [c.poi.category for c in out[:3]] == ["cafe", "cafe", "cafe"]


def test_at_most_one_unnamed_playground_in_top3() -> None:
    pois = [
        make_poi(str(i), "playground", 0.0005 * i, name="Детская площадка", tags=["kids"])
        for i in range(1, 6)
    ] + [
        make_poi("10", "museum", 0.004, name="Музей", tags=["kids"]),
        make_poi("11", "park", 0.005, name="Парк Влюблённых", tags=["kids"]),
    ]
    out = retrieve(make_store(pois), intent("с ребёнком", with_kids=True), make_ctx(), None, k=5)
    top3 = [c.poi.name for c in out[:3]]
    assert top3.count("Детская площадка") == 1


def test_mmr_value_recorded(synthetic_pois: list[POI]) -> None:
    out = retrieve(make_store(synthetic_pois), intent(), make_ctx(), None, k=5)
    assert all("mmr" in c.score_parts for c in out)


# ------------------------------------------------------------- misc / API


def test_k_limit_and_determinism(synthetic_pois: list[POI]) -> None:
    store = make_store(synthetic_pois)
    first = retrieve(store, intent("кофе"), make_ctx(), None, k=3)
    second = retrieve(store, intent("кофе"), make_ctx(), None, k=3)
    assert len(first) == 3
    assert ids(first) == ids(second)


def test_empty_store() -> None:
    assert retrieve(make_store([]), intent(), make_ctx(), None) == []


def test_matched_starts_with_walk(synthetic_pois: list[POI]) -> None:
    out = retrieve(make_store(synthetic_pois), intent(), make_ctx(), None, k=8)
    for c in out:
        assert c.matched[0] == f"≈{c.walk_min} мин пешком"
        assert len(c.matched) <= 4


def test_explain_other_languages(synthetic_pois: list[POI]) -> None:
    q = intent("coffee", must_tags=["coffee"], lang="en")
    out = retrieve(make_store(synthetic_pois), q, make_ctx(), None, k=3)
    en = explain(out[0], q, make_ctx(), lang="en")
    assert en[0] == f"≈{out[0].walk_min} min walk"
    kk = explain(out[0], q, make_ctx(), lang="kk")
    assert kk[0] == f"≈{out[0].walk_min} мин жаяу"


# ------------------------------------------------------------ lexical unit


def test_lexical_relevance_bounds() -> None:
    assert lexical_relevance(intent("кофе"), "") == 0.0
    assert lexical_relevance(intent("и в на"), "кафе кофе") == 0.0
    score = lexical_relevance(intent("кофе"), "Кофейня у дома кофе")
    assert 0.0 < score <= 1.0


def test_lexical_relevance_stemming() -> None:
    text = "Детская площадка playground kids дети детская площадка"
    assert lexical_relevance(intent("детские площадки"), text) == pytest.approx(1.0)
    assert lexical_relevance(intent("музей"), text) == 0.0


def test_lexical_relevance_tag_bonus() -> None:
    q = intent("где посидеть", must_tags=["coffee", "power_outlets"])
    with_tags = lexical_relevance(q, "кафе coffee power_outlets")
    without = lexical_relevance(q, "кафе")
    assert with_tags > without


# ------------------------------------------------- real OSM fixture (T10, DATA lane)

SAMPLE = FIXTURES / "poi_sample.jsonl"
needs_sample = pytest.mark.skipif(
    not SAMPLE.exists(), reason="tests/fixtures/poi_sample.jsonl not merged yet (T10)"
)
TUE_AFTERNOON = datetime(2026, 10, 20, 15, 0, tzinfo=ASTANA_TZ)


def landmark_ctx(landmarks: dict[str, Landmark], landmark_id: str = "expo") -> SearchContext:
    origin = landmarks[landmark_id].geo
    return SearchContext(
        origin=origin,
        h3_cell=to_h3(origin),
        h3_res=9,
        area_label=f"у {landmark_id}",
        landmark_id=landmark_id,
        local_time=TUE_AFTERNOON,
        radius_m=1200,
    )


@needs_sample
def test_real_expo_landmark_close_to_reference(landmarks: dict[str, Landmark]) -> None:
    # DATA lane re-checks landmarks against OSM; allow ±100 m drift
    assert haversine_m(landmarks["expo"].geo, ORIGIN) <= 100


@needs_sample
def test_real_s2_quiet_coffee_near_expo(landmarks: dict[str, Landmark]) -> None:
    q = intent(
        "Хочу тихое место с кофе и розеткой недалеко от меня.",
        must_tags=["coffee", "power_outlets", "quiet"],
    )
    out = retrieve(make_store(load_pois(SAMPLE)), q, landmark_ctx(landmarks), None, k=8)
    assert out[0].poi.category == "cafe"
    assert any(m.startswith("есть кофе") for m in out[0].matched)


@pytest.mark.parametrize("categories", [[], ["playground", "museum"]])
@needs_sample
def test_real_s3_kids_near_expo(landmarks: dict[str, Landmark], categories: list[str]) -> None:
    q = intent(
        "Еду с ребёнком — где рядом есть детские площадки или музей с интерактивами?",
        with_kids=True,
        categories=categories,
    )
    ctx = landmark_ctx(landmarks)
    out = retrieve(make_store(load_pois(SAMPLE)), q, ctx, None, k=8)
    assert out[0].poi.category in {"playground", "museum"}
    if not categories:  # alternatives exist: diversity rule applies to the candidate list
        assert [c.poi.name for c in out[:3]].count("Детская площадка") <= 1
    cards = template_cards(out, q, ctx)  # cards never repeat an unnamed place
    assert [c.title for c in cards].count("Детская площадка") == 1
