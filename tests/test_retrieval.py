"""T22: retrieval — hard filters, scoring, MMR diversity, deterministic reasons."""

from __future__ import annotations

from datetime import datetime

import numpy as np
import pytest

from app.geo import haversine_m, radius_for, to_h3, walk_minutes
from app.hours import ASTANA_TZ
from app.retrieval import (
    INFO_PARTS,
    WEIGHTS,
    explain,
    hours_dropped,
    lexical_relevance,
    query_unmatched,
    retrieve,
)
from app.route import plan_route
from app.schemas import POI, GeoPoint, Landmark, QueryIntent, SearchContext, WeatherInfo
from app.store import Store, load_pois
from app.validate import template_cards

from .conftest import EXPO, FIXTURES, ROOT

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
    # T70: a full keyword match must beat a popular place with confirmed hours
    assert out[0].poi.id == "test:poi/5"


def test_weights_sum_to_one() -> None:
    assert sum(WEIGHTS.values()) == pytest.approx(1.0)
    assert WEIGHTS["relevance"] > 0.5  # relevance dominates the score


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
        bonuses = sum(v for k, v in parts.items() if k not in WEIGHTS and k not in INFO_PARTS)
        expected = sum(WEIGHTS[k] * parts[k] for k in WEIGHTS) + bonuses
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
    assert sum(c.title.startswith("Детская площадка") for c in cards) == 1


# --------------------------------------------------------- review follow-ups


def test_bar_query_is_not_a_stopword(synthetic_pois: list[POI]) -> None:
    evening = datetime(2026, 10, 20, 20, 0, tzinfo=ASTANA_TZ)
    out = retrieve(
        make_store(synthetic_pois), intent("хочу в бар"), make_ctx(at=evening), None, k=8
    )
    bar = next(c for c in out if c.poi.id == "test:poi/8")
    assert bar.score_parts["relevance"] > 0


def test_non_finite_query_vector_falls_back_to_lexical(synthetic_pois: list[POI]) -> None:
    store = make_store(synthetic_pois, embeddings=np.eye(len(synthetic_pois), 4, dtype=np.float32))
    bad = np.array([np.nan, 0, 0, 0], dtype=np.float32)
    out = retrieve(store, intent("кофе"), make_ctx(), bad, k=5)
    assert all(np.isfinite(c.score) for c in out)


def test_poi_without_vector_gets_pool_median(synthetic_pois: list[POI]) -> None:
    """Silent-failure review №2: a POI without a vector was scored on the lexical scale only,
    while the others got 0.7*cos + 0.3*lex — different scales. Now it gets the pool median."""
    matrix = np.zeros((len(synthetic_pois), 4), dtype=np.float32)
    for i in range(len(synthetic_pois)):
        matrix[i] = [0.6, 0.8, 0.0, 0.0] if i % 2 else [0.8, 0.6, 0.0, 0.0]
    alpha = next(i for i, p in enumerate(synthetic_pois) if p.id == "test:poi/1")
    matrix[alpha] = 0.0  # no vector for this POI
    store = make_store(synthetic_pois, embeddings=matrix)
    q = intent("кофе", must_tags=["coffee"])
    out = retrieve(store, q, make_ctx(), np.array([1, 0, 0, 0], dtype=np.float32), k=20)
    alpha_c = next(c for c in out if c.poi.id == "test:poi/1")
    others = [c.score_parts["semantic"] for c in out if c.poi.id != "test:poi/1"]
    assert "semantic" not in alpha_c.score_parts
    assert alpha_c.score_parts["no_vector"] == 1.0
    lexical = retrieve(make_store(synthetic_pois), q, make_ctx(), None, k=20)
    alpha_lex = next(c for c in lexical if c.poi.id == "test:poi/1").score_parts["relevance"]
    median = float(np.median(others))
    assert alpha_c.score_parts["relevance"] == pytest.approx(0.7 * median + 0.3 * alpha_lex,
                                                              abs=1e-3)


def test_unnamed_places_do_not_flood_candidates() -> None:
    pois = [
        make_poi(str(i), "playground", 0.0001 * i, name="Детская площадка", tags=["kids"])
        for i in range(1, 31)
    ] + [
        make_poi(f"m{i}", "museum", 0.004 + 0.0005 * i, name=f"Музей {i}", tags=["kids"])
        for i in range(3)
    ]
    out = retrieve(make_store(pois), intent("с ребёнком", with_kids=True), make_ctx(), None, k=8)
    assert [c.poi.name for c in out].count("Детская площадка") == 1
    assert sum(c.poi.category == "museum" for c in out) == 3


def test_kids_reason_only_when_data_says_so() -> None:
    pois = [make_poi("1", "mall"), make_poi("2", "mall", kids_friendly=True)]
    out = retrieve(make_store(pois), intent(with_kids=True), make_ctx(), None, k=5)
    reasons = {c.poi.id: c.matched for c in out}
    assert "подходит детям" not in reasons["test:poi/1"]  # category guess, not data
    assert "подходит детям" in reasons["test:poi/2"]


def test_retrieve_is_fast_enough() -> None:
    import time

    pois = [
        make_poi(str(i), ["cafe", "park", "museum", "playground"][i % 4], 0.00002 * i,
                 opening_hours="Mo-Fr 09:00-18:00; Sa 10:00-16:00" if i % 3 else None,
                 tags=["coffee", "indoor"] if i % 4 == 0 else ["outdoor"])
        for i in range(5000)
    ]
    store = make_store(pois)
    q = intent("тихое место с кофе и розеткой", must_tags=["coffee", "quiet"])
    started = time.perf_counter()
    retrieve(store, q, make_ctx(radius=2500), None, k=8)
    assert time.perf_counter() - started < 1.0


# ------------------------------------------- full OSM dump (T11: 531 POI, 5 demo zones)
# Data facts: hours known for ~28% of places, wheelchair for ~1%, 110 of 111 playgrounds
# are unnamed «Детская площадка», the airport zone has only a handful of objects.

FULL = ROOT / "data" / "processed" / "poi.jsonl"
needs_full = pytest.mark.skipif(not FULL.exists(), reason="data/processed/poi.jsonl not merged")


@pytest.fixture(scope="module")
def full_store() -> Store:
    return make_store(load_pois(FULL))


def zone_ctx(
    landmarks: dict[str, Landmark], landmark_id: str, q: QueryIntent, at: datetime = TUE_AFTERNOON
) -> SearchContext:
    origin = landmarks[landmark_id].geo
    return SearchContext(
        origin=origin,
        h3_cell=to_h3(origin),
        h3_res=9,
        area_label=landmark_id,
        landmark_id=landmark_id,
        local_time=at,
        radius_m=radius_for(q),
    )


@needs_full
def test_full_s1_walk_route_fits_budget(
    full_store: Store, landmarks: dict[str, Landmark]
) -> None:
    q = intent("Я сейчас возле Mega Silk Way, хочу прогуляться 30–60 минут", time_budget_min=60)
    ctx = zone_ctx(landmarks, "mega_silk_way", q)
    cards = template_cards(retrieve(full_store, q, ctx, None, k=8), q, ctx)
    assert len(cards) == 3
    stops = [c for c in retrieve(full_store, q, ctx, None, k=8) if c.poi.id in
             {card.poi_id for card in cards}]
    route = plan_route(ctx.origin, stops, 60, {c.poi_id: c.visit_min_low for c in cards})
    assert route is not None
    assert len(route.stop_ids) >= 2
    assert route.fits_budget


@needs_full
def test_full_s2_cafes_near_expo(full_store: Store, landmarks: dict[str, Landmark]) -> None:
    q = intent("Хочу тихое место с кофе и розеткой", must_tags=["coffee", "power_outlets", "quiet"])
    out = retrieve(full_store, q, zone_ctx(landmarks, "expo", q), None, k=8)
    assert [c.poi.category for c in out[:2]] == ["cafe", "cafe"]


@needs_full
def test_full_s3_kids_one_unnamed_playground(
    full_store: Store, landmarks: dict[str, Landmark]
) -> None:
    q = intent("Еду с ребёнком — где детские площадки или музей?", with_kids=True)
    ctx = zone_ctx(landmarks, "expo", q)
    out = retrieve(full_store, q, ctx, None, k=8)
    assert out[0].poi.category in {"playground", "museum"}
    assert [c.poi.name for c in out].count("Детская площадка") <= 1
    cards = template_cards(out, q, ctx)
    assert len(cards) >= 2


@needs_full
def test_full_sparse_hours_are_honest(full_store: Store, landmarks: dict[str, Landmark]) -> None:
    q = intent("куда сходить")
    ctx = zone_ctx(landmarks, "baiterek", q)
    out = retrieve(full_store, q, ctx, None, k=8)
    assert out
    for c in out:
        assert c.hours.status in {"open", "unknown"}  # closed places are filtered out
        if c.hours.status == "unknown":
            assert "часы работы не указаны" in c.matched
    for card in template_cards(out, q, ctx):
        if card.open_status == "unknown":
            assert card.open_note == "нет данных о часах, проверьте перед визитом"


@needs_full
def test_full_wheelchair_mostly_unconfirmed(
    full_store: Store, landmarks: dict[str, Landmark]
) -> None:
    q = intent("кафе", wheelchair=True)
    out = retrieve(full_store, q, zone_ctx(landmarks, "expo", q), None, k=8)
    assert out  # unknown accessibility is kept, never silently dropped
    for c in out:
        assert any(m.startswith(("доступно", "частично", "доступность не")) for m in c.matched)


@needs_full
def test_full_airport_zone_is_sparse_but_safe(
    full_store: Store, landmarks: dict[str, Landmark]
) -> None:
    q = intent("2 часа до вылета, что посмотреть рядом", time_budget_min=120, tourist=True)
    ctx = zone_ctx(landmarks, "airport_nqz", q)
    out = retrieve(full_store, q, ctx, None, k=8)
    assert 1 <= len(out) <= 8
    cards = template_cards(out, q, ctx)
    assert 1 <= len(cards) <= 3


def test_landmark_words_are_not_relevance() -> None:
    """«Я возле Mega Silk Way, хочу прогуляться»: the mall is where the user stands,
    its name in the query must not make it the best match."""
    from app.schemas import Landmark as LandmarkModel

    mall = make_poi("1", "mall", 0.0, name="Mega Silk Way", opening_hours="Mo-Su 10:00-22:00",
                    popularity=0.3)
    park = make_poi("2", "park", 0.004, name="NU Park", tags=["walk", "outdoor"])
    mega = LandmarkModel(id="mega_silk_way", names={"ru": "ТРЦ Mega Silk Way"},
                         aliases=["mega silk way", "mega"], geo=mall.geo)
    store = Store(pois=[mall, park], by_id={p.id: p for p in (mall, park)},
                  landmarks={"mega_silk_way": mega})
    q = intent("Я сейчас возле Mega Silk Way, хочу прогуляться", must_tags=["walk"],
               landmark_mention="mega_silk_way")
    ctx = make_ctx().model_copy(update={"origin": mall.geo, "landmark_id": "mega_silk_way"})
    out = retrieve(store, q, ctx, None, k=2)
    assert out[0].poi.id == "test:poi/2"
    assert next(c for c in out if c.poi.id == "test:poi/1").score_parts["relevance"] == 0.0


def test_night_prefers_places_open_longer() -> None:
    late = datetime(2026, 10, 19, 23, 15, tzinfo=ASTANA_TZ)
    soon = make_poi("1", "restaurant", 0.0010, opening_hours="Mo-Su 10:00-24:00", tags=["food"])
    long = make_poi("2", "restaurant", 0.0012, opening_hours="Mo-Su 12:00-02:00", tags=["food"])
    q = intent("поздно вечером поесть", night=True, must_tags=["food"])
    out = retrieve(make_store([soon, long]), q, make_ctx(at=late), None, k=2)
    assert out[0].poi.id == "test:poi/2"
    closing = next(c for c in out if c.poi.id == "test:poi/1")
    assert closing.hours.closes_in_min == 45
    assert closing.score_parts["open"] < out[0].score_parts["open"]


def test_group_prefers_places_for_a_company() -> None:
    cafe = make_poi("1", "cafe", 0.0010, tags=["coffee"])
    rest = make_poi("2", "restaurant", 0.0011, tags=["food"])
    lib = make_poi("3", "library", 0.0009)
    q = intent("Мы компанией из 5 человек, чем заняться вечером?", group_size=5)
    out = retrieve(make_store([cafe, rest, lib]), q, make_ctx(), None, k=3)
    assert out[0].poi.id == "test:poi/2"
    parts = {c.poi.id: c.score_parts.get("group", 0.0) for c in out}
    assert parts["test:poi/2"] > 0 > parts["test:poi/3"]
    assert parts["test:poi/1"] == 0.0


def test_no_group_part_for_small_or_unknown_groups() -> None:
    rest = make_poi("2", "restaurant", 0.0011, tags=["food"])
    for size in (None, 2):
        out = retrieve(make_store([rest]), intent("поесть", group_size=size), make_ctx(), None)
        assert "group" not in out[0].score_parts


@pytest.mark.parametrize(
    ("lang", "reason"),
    [
        ("ru", "нет данных: розетки, Wi-Fi"),
        ("kk", "дерек жоқ: розетка, Wi-Fi"),
        ("en", "no data: power outlets, Wi-Fi"),
    ],
)
def test_requested_features_missing_in_data_are_named(lang: str, reason: str) -> None:
    cafe = make_poi("1", "cafe", 0.001, tags=["coffee"])
    q = intent("кофе, поработать, нужна розетка и wifi", lang=lang,
               must_tags=["coffee", "power_outlets", "wifi"])
    out = retrieve(make_store([cafe]), q, make_ctx(), None)
    assert reason in explain(out[0], q, make_ctx(), lang=lang)
    assert out[0].matched[1].startswith("есть кофе")  # what the data has comes first


@pytest.mark.parametrize(
    ("tag", "reason"),
    [("food", "serves food"), ("shopping", "shops inside"), ("history", "history spot")],
)
def test_english_reasons_read_as_phrases(tag: str, reason: str) -> None:
    poi = make_poi("1", "restaurant", 0.001, tags=[tag])
    q = intent("food", must_tags=[tag], lang="en")
    out = retrieve(make_store([poi]), q, make_ctx(), None)
    assert reason in explain(out[0], q, make_ctx(), lang="en")


def test_night_needs_confirmed_hours_or_late_tags() -> None:
    """E2 with Claude: bars tagged only `night` with unknown hours were shown at 02:30."""
    at = datetime(2026, 10, 20, 2, 30, tzinfo=ASTANA_TZ)
    pois = [
        make_poi("1", "bar", 0.0010, tags=["night", "indoor"]),  # unknown hours, no late tag
        make_poi("2", "bar", 0.0012, tags=["late_open"]),  # unknown hours, late tag
        make_poi("3", "bar", 0.0014, opening_hours="Mo-Su 18:00-04:00"),  # open/high at 02:30
        make_poi("4", "viewpoint", 0.0016, tags=["24_7"]),
    ]
    q = intent("ночью в бар", night=True, categories=["bar", "nightlife", "viewpoint"],
               must_tags=["night", "late_open"])
    out = retrieve(make_store(pois), q, make_ctx(at=at), None, k=5)
    assert set(ids(out)) == {"test:poi/2", "test:poi/3", "test:poi/4"}


def test_place_without_any_requested_tag_does_not_jump_the_cafes() -> None:
    """S2 near EXPO with DATA e2b82fe: the sphere museum got confirmed hours, sits at 0 m and
    is popular, but has none of coffee / power_outlets / quiet; MMR must not pull it to #2."""
    tue = datetime(2026, 10, 20, 15, 0, tzinfo=ASTANA_TZ)
    cafes = [
        make_poi(str(i), "cafe", 0.0043 + 0.0003 * i, tags=["coffee", "indoor"],
                 opening_hours="Mo-Su 08:00-22:00", data_confidence=0.55, popularity=0.1)
        for i in range(1, 4)
    ]
    museum = make_poi("9", "museum", 0.0, name="Alem.ai", tags=["indoor"], indoor=True,
                      opening_hours="Tu-Su 10:00-18:30; Mo off", popularity=0.61,
                      data_confidence=0.75)
    filler = [  # weak far places, as in the real pool: they set the low end of MMR scaling
        make_poi(f"f{i}", "monument", 0.0090 + 0.0002 * i, data_confidence=0.3) for i in range(3)
    ]
    q = intent("Хочу тихое место с кофе и розеткой", must_tags=["coffee", "power_outlets", "quiet"])
    out = retrieve(make_store([*cafes, museum, *filler]), q, make_ctx(at=tue), None, k=4)
    assert [c.poi.category for c in out[:2]] == ["cafe", "cafe"]
    assert next(c for c in out if c.poi.id == "test:poi/9").score_parts["no_tags"] < 0


def test_no_tags_penalty_only_when_tags_were_asked() -> None:
    museum = make_poi("9", "museum", 0.0, tags=["indoor"])
    out = retrieve(make_store([museum]), intent("куда сходить"), make_ctx(), None)
    assert "no_tags" not in out[0].score_parts


# ------------------------------------------------ RAG review R7: "no match" signal


@pytest.mark.parametrize(
    ("query", "kwargs", "expected"),
    [
        ("порекомендуй казино", {}, True),
        ("абракадабра", {}, True),
        ("кофе", {}, False),  # cafes match the word
        ("куда сходить", {}, False),  # only stopwords: nothing was asked specifically
        ("казино", {"must_tags": ["night"]}, False),  # structured request, not free text
        ("казино", {"categories": ["bar"]}, False),
    ],
)
def test_query_unmatched(
    synthetic_pois: list[POI], query: str, kwargs: dict[str, object], expected: bool
) -> None:
    store = make_store(synthetic_pois)
    q = intent(query, **kwargs)
    out = retrieve(store, q, make_ctx(), None, k=8)
    assert query_unmatched(store, q, make_ctx(), out) is expected


def test_query_unmatched_ignores_landmark_words(synthetic_pois: list[POI]) -> None:
    from app.schemas import Landmark as LandmarkModel

    mega = LandmarkModel(id="mega_silk_way", names={"ru": "Mega"}, aliases=["mega silk way"],
                         geo=ORIGIN)
    store = Store(pois=synthetic_pois, by_id={p.id: p for p in synthetic_pois},
                  landmarks={"mega_silk_way": mega})
    q = intent("я возле mega silk way", landmark_mention="mega_silk_way")
    ctx = make_ctx().model_copy(update={"landmark_id": "mega_silk_way"})
    assert query_unmatched(store, q, ctx, retrieve(store, q, ctx, None)) is False
    assert query_unmatched(store, intent("казино"), make_ctx(), []) is False  # nothing found



def test_semantic_part_only_when_cosine_was_used(synthetic_pois: list[POI]) -> None:
    matrix = np.tile(np.array([0.6, 0.8, 0.0, 0.0], dtype=np.float32), (len(synthetic_pois), 1))
    store = make_store(synthetic_pois, embeddings=matrix)
    good = retrieve(store, intent("кофе"), make_ctx(), np.array([1, 0, 0, 0], np.float32))
    assert all("semantic" in c.score_parts for c in good)
    for query_vec in (None, np.ones(3, np.float32), np.array([np.nan, 0, 0, 0], np.float32)):
        out = retrieve(store, intent("кофе"), make_ctx(), query_vec)
        assert not any("semantic" in c.score_parts for c in out)  # vector was not used


# ------------------------------------- silent-failure review №2, item 4: hours drops


def test_hours_dropped_counts_reasons(synthetic_pois: list[POI]) -> None:
    store = make_store(synthetic_pois)
    monday = hours_dropped(store, intent(), make_ctx(at=MON_NOON, radius=1500))
    assert monday["closed"] == 2  # museum «Mo off» + bar (opens at 18:00)
    assert monday["closed_partial"] == 1  # gallery: Monday not described in OSM
    late = datetime(2026, 10, 20, 21, 45, tzinfo=ASTANA_TZ)
    assert hours_dropped(store, intent(), make_ctx(at=late))["closing_soon"] >= 2
    night = datetime(2026, 10, 20, 1, 0, tzinfo=ASTANA_TZ)
    assert hours_dropped(store, intent(night=True), make_ctx(at=night))["night"] >= 1
    # consistent with retrieve: dropped + kept = everything that passed radius/profile
    out = retrieve(store, intent(), make_ctx(at=MON_NOON, radius=1500), None, k=50)
    in_radius = [p for p in synthetic_pois if haversine_m(ORIGIN, p.geo) <= 1500]
    assert len(out) + sum(monday.values()) == len(in_radius)
