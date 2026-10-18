"""app/pipeline.py end to end with CORE fakes and a fake LLM (no network).

CORE modules (geo, retrieval, validate, route) belong to another lane: the fixture
`core_fakes` swaps them for small deterministic fakes that follow the stub signatures.
`test_real_core_if_implemented` runs the same flow on the real modules once they exist.
test_api.py imports the helpers below (FakeLLM, make_store, install_core_fakes, ...).
"""

from __future__ import annotations

import dataclasses
import logging
import math
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import h3
import numpy as np
import pytest

from app import geo, hours, retrieval, route, validate, weather
from app.config import Settings, get_settings
from app.generate import build_prompt
from app.llm import LLMUnavailable
from app.pipeline import Deps, InvalidRequest, UnknownLandmarkError, recommend
from app.schemas import (
    POI,
    Candidate,
    Card,
    CardDraft,
    DraftResponse,
    GeoPoint,
    HoursVerdict,
    Landmark,
    MiniRoute,
    QueryIntent,
    RecommendRequest,
    RouteLeg,
    SearchContext,
    WeatherInfo,
)
from app.store import Store

from .conftest import ASTANA_TZ

# ------------------------------------------------------------------ CORE fakes


def _dist(a: GeoPoint, b: GeoPoint) -> float:
    dy = (a.lat - b.lat) * 111_320
    dx = (a.lon - b.lon) * 111_320 * math.cos(math.radians(a.lat))
    return math.hypot(dx, dy)


def _walk(d: float) -> int:
    return max(1, math.ceil(d * 1.3 / 75))


def fake_resolve_origin(loc: Any, landmarks: dict[str, Landmark]
                        ) -> tuple[GeoPoint, str, str | None, str]:
    if loc.landmark_id is not None:
        lm = landmarks[loc.landmark_id]
        return lm.geo, h3.latlng_to_cell(lm.geo.lat, lm.geo.lon, 9), lm.id, f"у {lm.names['ru']}"
    lat, lon = h3.cell_to_latlng(loc.h3_cell)
    return GeoPoint(lat=lat, lon=lon), loc.h3_cell, None, "ваш район (≈0,1 км²)"


def fake_match_landmark(text: str, landmarks: dict[str, Landmark]) -> Landmark | None:
    low = text.lower()
    return next((lm for lm in landmarks.values() if any(a in low for a in lm.aliases)), None)


def fake_retrieve(store: Store, intent: QueryIntent, ctx: SearchContext,
                  query_vec: np.ndarray | None, k: int = 8) -> list[Candidate]:
    out = []
    for poi in store.pois:
        d = _dist(ctx.origin, poi.geo)
        if d > ctx.radius_m or (intent.categories and poi.category not in intent.categories):
            continue
        status = "unknown" if not poi.opening_hours else "open"
        verdict = HoursVerdict(status=status, confidence="low" if status == "unknown" else "high",
                               note="нет данных о часах работы" if status == "unknown"
                               else f"по данным OSM: {poi.opening_hours}")
        out.append(Candidate(poi=poi, distance_m=round(d), walk_min=_walk(d), hours=verdict,
                             score=1 - d / ctx.radius_m, matched=[f"≈{_walk(d)} мин пешком"]))
    return sorted(out, key=lambda c: -c.score)[:k]


def _card(c: Candidate, rank: int, why: str, todo: str, lo: int, hi: int, src: str) -> Card:
    p = c.poi
    return Card(rank=rank, poi_id=p.id, title=p.name, category=p.category, geo=p.geo,
                distance_m=c.distance_m, walk_min=c.walk_min, visit_min_low=lo,
                visit_min_high=hi, why=why, what_to_do=todo, open_status=c.hours.status,
                open_confidence=c.hours.confidence, open_note=c.hours.note, idea_source=src,
                facts_sources=p.sources, data_confidence=p.data_confidence, tags=p.tags)


def fake_template_cards(candidates: list[Candidate], intent: QueryIntent, ctx: SearchContext,
                        n: int = 3) -> list[Card]:
    return [_card(c, i + 1, "; ".join(c.matched), "Зайдите.", 20, 40, "poi_data")
            for i, c in enumerate(candidates[:n])]


def fake_finalize(draft: DraftResponse, candidates: list[Candidate], intent: QueryIntent,
                  ctx: SearchContext) -> tuple[list[Card], list[str]]:
    by_id = {c.poi.id: c for c in candidates}
    kept = [d for d in draft.cards if d.poi_id in by_id]
    if not kept:
        return fake_template_cards(candidates, intent, ctx), ["шаблонные карточки"]
    cards = [_card(by_id[d.poi_id], i + 1, d.why, d.what_to_do, d.visit_min_low,
                   d.visit_min_high, d.idea_source) for i, d in enumerate(kept[:3])]
    return cards, []


def fake_plan_route(origin: GeoPoint, stops: list[Candidate], budget_min: int | None,
                    visit_min: dict[str, int], max_stops: int = 3) -> MiniRoute | None:
    if not stops:
        return None
    legs, prev, prev_id = [], origin, "origin"
    for s in stops[:max_stops]:
        d = round(_dist(prev, s.poi.geo))
        legs.append(RouteLeg(from_id=prev_id, to_id=s.poi.id, distance_m=d, walk_min=_walk(d)))
        prev, prev_id = s.poi.geo, s.poi.id
    total = sum(leg.walk_min for leg in legs) + sum(visit_min.values())
    return MiniRoute(stop_ids=[s.poi.id for s in stops[:max_stops]], legs=legs,
                     total_walk_m=sum(leg.distance_m for leg in legs), total_min=total,
                     fits_budget=budget_min is None or total <= budget_min,
                     path=[origin] + [s.poi.geo for s in stops[:max_stops]],
                     note="Маршрут приблизительный: прямые отрезки × 1,3")


def install_core_fakes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(geo, "resolve_origin", fake_resolve_origin)
    monkeypatch.setattr(geo, "radius_for", lambda intent: 1200)
    monkeypatch.setattr(geo, "match_landmark", fake_match_landmark)
    monkeypatch.setattr(retrieval, "retrieve", fake_retrieve)
    monkeypatch.setattr(validate, "finalize", fake_finalize)
    monkeypatch.setattr(validate, "template_cards", fake_template_cards)
    monkeypatch.setattr(route, "plan_route", fake_plan_route)
    monkeypatch.setattr(weather, "get_weather", lambda origin, at: None)


# ------------------------------------------------------------- shared helpers


class FakeLLM:
    """Duck-typed LLMClient: answers QueryIntent / DraftResponse, records calls."""

    def __init__(self, *, available: bool = True, fail: Exception | None = None,
                 intent: QueryIntent | None = None, embed: np.ndarray | None = None) -> None:
        self.available = available
        self.fail = fail
        self.intent = intent
        self.embed = embed
        self.model_name = "fake-claude"
        self.last_model: str | None = None
        self.calls: list[str] = []
        self.embedded: list[str] = []

    def generate_structured(self, *, system: str, prompt: str, schema: type, effort: str = "low"
                            ) -> Any:
        self.calls.append(schema.__name__)
        if self.fail is not None:
            raise self.fail
        self.last_model = "fake-claude"
        if schema is QueryIntent:
            return self.intent or QueryIntent(raw_query="x", parser="llm")
        ids = [line.split('"')[3] for line in prompt.splitlines() if line.strip()
               .startswith('"id":')]
        return DraftResponse(summary="Идеи от модели", cards=[
            CardDraft(poi_id=i, why="Рядом и подходит.", what_to_do="Загляните на 20 минут.",
                      visit_min_low=20, visit_min_high=30, idea_source="generated")
            for i in ids[:2]])

    def embed_query(self, text: str) -> np.ndarray | None:
        self.embedded.append(text)
        return self.embed


def make_settings(tmp_path: Path) -> Settings:
    return dataclasses.replace(get_settings(), cache_dir=tmp_path / "cache", h3_res=9)


def make_store(pois: list[POI], landmarks: dict[str, Landmark], **over: Any) -> Store:
    return Store(pois=pois, by_id={p.id: p for p in pois}, landmarks=landmarks,
                 snapshot_date="2026-10-18", **over)


def req(query: str = "кофе", **kw: Any) -> RecommendRequest:
    body: dict[str, Any] = {"query": query, "location": {"landmark_id": "expo"}}
    body.update(kw)
    return RecommendRequest.model_validate(body)


@pytest.fixture
def deps(synthetic_pois: list[POI], landmarks: dict[str, Landmark], tmp_path: Path) -> Deps:
    return Deps(store=make_store(synthetic_pois, landmarks), llm=FakeLLM(),
                settings=make_settings(tmp_path))


@pytest.fixture
def core_fakes(monkeypatch: pytest.MonkeyPatch) -> None:
    install_core_fakes(monkeypatch)


# ---------------------------------------------------------------------- tests


@pytest.mark.usefixtures("core_fakes")
class TestRecommend:
    def test_llm_path(self, deps: Deps) -> None:
        a = recommend(req("Хочу кофе у EXPO"), deps)
        assert a.data_used.generation == "llm" and a.data_used.parser == "llm"
        assert a.data_used.model == "fake-claude"
        assert a.summary == "Идеи от модели"
        assert 1 <= len(a.cards) <= 3
        assert [c.rank for c in a.cards] == list(range(1, len(a.cards) + 1))
        assert a.context.landmark_id == "expo" and a.context.h3_res == 9
        assert a.data_used.candidates_considered >= len(a.cards)
        assert a.lang == "ru" and a.disclaimer.startswith("Подсказки")
        assert len(a.request_id) == 12 and a.latency_ms >= 0
        assert deps.llm.calls == ["QueryIntent", "DraftResponse"]

    def test_no_ai_mode_is_deterministic(self, deps: Deps) -> None:
        a = recommend(req("кофе", use_llm=False), deps)
        assert a.data_used.generation == "template" and a.data_used.parser == "rules"
        assert a.data_used.model is None
        assert deps.llm.calls == [] and deps.llm.embedded == []
        assert all(c.idea_source == "poi_data" for c in a.cards)
        assert "режим без ИИ" in a.data_used.filters
        assert not any("LLM" in w for w in a.warnings)  # the user chose it, not a failure

    def test_llm_down_falls_back_with_warning(self, deps: Deps) -> None:
        deps.llm = FakeLLM(fail=LLMUnavailable("quota"))
        a = recommend(req(), deps)
        assert a.data_used.generation == "template" and a.data_used.parser == "rules"
        assert "LLM недоступна — показаны карточки по данным." in a.warnings
        assert a.cards

    def test_llm_unavailable_flag_skips_calls(self, deps: Deps) -> None:
        deps.llm = FakeLLM(available=False)
        a = recommend(req(), deps)
        assert deps.llm.calls == []
        assert a.data_used.generation == "template"
        assert a.warnings.count("LLM недоступна — показаны карточки по данным.") == 1

    def test_no_llm_object(self, deps: Deps) -> None:
        deps.llm = None
        assert recommend(req(), deps).data_used.generation == "template"

    def test_finalize_fallback_is_reported_as_template(self, deps: Deps,
                                                       monkeypatch: pytest.MonkeyPatch) -> None:
        def bad_ids(*, schema: type, **_k: Any) -> Any:
            if schema is QueryIntent:
                return QueryIntent(raw_query="x", parser="llm")
            return DraftResponse(summary="s", cards=[CardDraft(
                poi_id="osm:node/1", why="w", what_to_do="t", visit_min_low=10,
                visit_min_high=20, idea_source="generated")])

        monkeypatch.setattr(deps.llm, "generate_structured", bad_ids)
        a = recommend(req(), deps)
        assert a.data_used.generation == "template"
        assert "шаблонные карточки" in a.warnings

    def test_unknown_landmark_is_invalid_input(self, deps: Deps) -> None:
        bad = RecommendRequest.model_validate(
            {"query": "кофе", "location": {"landmark_id": "nowhere"}})
        with pytest.raises(UnknownLandmarkError, match="nowhere"):
            recommend(bad, deps)

    def test_invalid_h3_cell_is_invalid_input(self, deps: Deps) -> None:
        bad = RecommendRequest.model_validate(
            {"query": "кофе", "location": {"h3_cell": "000000000000000"}})
        with pytest.raises(InvalidRequest):
            recommend(bad, deps)

    def test_fine_cell_is_coarsened_to_r9(self, deps: Deps) -> None:
        fine = h3.latlng_to_cell(51.0894, 71.4161, 13)
        a = recommend(RecommendRequest.model_validate(
            {"query": "кофе", "location": {"h3_cell": fine}, "use_llm": False}), deps)
        assert a.context.h3_res == 9
        assert a.context.h3_cell == h3.cell_to_parent(fine, 9)
        assert a.data_used.location.endswith("H3 r9")

    def test_radius_widened_once(self, deps: Deps, monkeypatch: pytest.MonkeyPatch) -> None:
        seen: list[int] = []

        def picky(store: Store, intent: QueryIntent, ctx: SearchContext, qv: Any, k: int = 8
                  ) -> list[Candidate]:
            seen.append(ctx.radius_m)
            return [] if ctx.radius_m < 1500 else fake_retrieve(store, intent, ctx, qv, k)

        monkeypatch.setattr(retrieval, "retrieve", picky)
        a = recommend(req(use_llm=False), deps)
        assert seen == [1200, 1800]
        assert a.context.radius_m == 1800
        assert "Рядом мало вариантов — радиус поиска расширен до 1800 м." in a.warnings

    def test_nothing_found_is_a_helpful_empty_answer(self, deps: Deps,
                                                     monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(retrieval, "retrieve", lambda *a, **k: [])
        a = recommend(req(), deps)
        assert a.cards == [] and a.route is None
        assert a.summary == "Рядом не нашлось подходящих мест."
        assert any("Подходящих мест не нашлось" in w for w in a.warnings)
        assert deps.llm.calls == ["QueryIntent"]  # no generation without candidates

    def test_retrieval_crash_does_not_raise(self, deps: Deps,
                                            monkeypatch: pytest.MonkeyPatch) -> None:
        def boom(*_a: Any, **_k: Any) -> list[Candidate]:
            raise NotImplementedError("T22 (CORE)")

        monkeypatch.setattr(retrieval, "retrieve", boom)
        a = recommend(req(), deps)
        assert a.cards == []
        assert "Поиск временно дал сбой — попробуйте ещё раз." in a.warnings

    def test_template_crash_does_not_raise(self, deps: Deps,
                                           monkeypatch: pytest.MonkeyPatch) -> None:
        def boom(*_a: Any, **_k: Any) -> list[Card]:
            raise RuntimeError("bug")

        monkeypatch.setattr(validate, "template_cards", boom)
        assert recommend(req(use_llm=False), deps).cards == []

    def test_route_for_two_or_more_cards(self, deps: Deps) -> None:
        a = recommend(req("кафе или музей"), deps)
        assert len(a.cards) >= 2 and a.route is not None
        assert a.route.stop_ids == [c.poi_id for c in a.cards][: len(a.route.stop_ids)]
        assert recommend(req("кафе", want_route=False), deps).route is None

    def test_route_crash_keeps_cards(self, deps: Deps, monkeypatch: pytest.MonkeyPatch) -> None:
        def boom(*_a: Any, **_k: Any) -> MiniRoute:
            raise RuntimeError("bug")

        monkeypatch.setattr(route, "plan_route", boom)
        a = recommend(req(use_llm=False), deps)
        assert a.cards and a.route is None

    def test_route_over_budget_warns(self, deps: Deps) -> None:
        a = recommend(req("есть 10 минут", use_llm=False), deps)
        assert a.route is not None and not a.route.fits_budget
        assert any("Маршрут не укладывается" in w for w in a.warnings)

    def test_context_warnings(self, deps: Deps) -> None:
        a = recommend(req("ночью с коляской, я турист", use_llm=False), deps)
        text = " | ".join(a.warnings)
        assert "Ночь:" in text and "коляски" in text and "аэропорт" in text

    def test_unknown_hours_are_counted(self, deps: Deps) -> None:
        a = recommend(req(use_llm=False), deps)
        unknown = sum(c.open_status == "unknown" for c in a.cards)
        if unknown == 1:
            assert "Для 1 места нет данных о часах работы — проверьте перед визитом." in a.warnings
        elif unknown > 1:
            assert (f"Для {unknown} мест нет данных о часах работы — проверьте перед визитом."
                    in a.warnings)

    def test_landmark_mismatch_warning(self, deps: Deps) -> None:
        a = recommend(req("я возле байтерек, кофе", use_llm=False), deps)
        assert any("Байтерек" in w and "выбранной точки" in w for w in a.warnings)
        assert a.context.landmark_id == "expo"

    def test_english_answer_texts(self, deps: Deps) -> None:
        a = recommend(req("quiet coffee please", use_llm=False), deps)
        assert a.lang == "en"
        assert a.disclaimer.startswith("Suggestions are based")
        assert a.summary.endswith("picked from open data.")
        assert a.data_used.filters[0].startswith("radius")

    def test_kazakh_disclaimer(self, deps: Deps) -> None:
        a = recommend(req("кофе", lang="kk", use_llm=False), deps)
        assert a.lang == "kk" and a.disclaimer.startswith("Ұсыныстар")

    def test_overrides_win(self, deps: Deps) -> None:
        a = recommend(req("кофе", use_llm=False,
                          overrides={"with_kids": True, "categories": ["museum"]}), deps)
        assert a.intent.with_kids and a.intent.categories == ["museum"]
        assert all(c.category == "museum" for c in a.cards)
        assert "с ребёнком" in a.data_used.filters

    def test_time_handling(self, deps: Deps) -> None:
        naive = recommend(req(at="2026-10-19T23:30:00", use_llm=False), deps)
        assert naive.context.local_time == datetime(2026, 10, 19, 23, 30, tzinfo=ASTANA_TZ)
        assert "время задано вручную" in naive.data_used.time
        utc = recommend(req(at="2026-10-19T18:30:00+00:00", use_llm=False), deps)
        assert utc.context.local_time.utcoffset() == timedelta(hours=5)
        assert utc.context.local_time.hour == 23
        now = recommend(req(use_llm=False), deps)
        assert now.context.local_time.tzinfo is not None
        assert abs(now.context.local_time - hours.now_astana()) < timedelta(minutes=1)

    def test_weather_goes_to_context_and_data_used(self, deps: Deps,
                                                   monkeypatch: pytest.MonkeyPatch) -> None:
        wx = WeatherInfo(temp_c=-3, summary="-3°C, ветер 5 м/с", outdoor_ok=True,
                         fetched_at=datetime.now(UTC))
        monkeypatch.setattr(weather, "get_weather", lambda origin, at: wx)
        a = recommend(req(use_llm=False), deps)
        assert a.context.weather == wx
        assert a.data_used.weather == "-3°C, ветер 5 м/с"
        assert "open_meteo" in a.data_used.sources
        b = recommend(req(use_llm=False, lang="en"), deps)
        assert b.data_used.weather == "-3°C, ветер 5 м/с"
        monkeypatch.setattr(weather, "get_weather", lambda origin, at: None)
        assert recommend(req(use_llm=False), deps).data_used.weather == "погода неизвестна"

    def test_query_vector_only_with_index_and_ai(self, deps: Deps) -> None:
        deps.llm = FakeLLM(embed=np.ones(4, dtype=np.float32) / 2)
        recommend(req("кофе"), deps)
        assert deps.llm.embedded == []  # store has no index
        deps.store.embeddings = np.zeros((len(deps.store.pois), 4), dtype=np.float32)
        recommend(req("кофе"), deps)
        assert deps.llm.embedded == ["кофе"]
        recommend(req("кофе", use_llm=False), deps)
        assert deps.llm.embedded == ["кофе"]

    def test_logs_never_carry_query_or_cell(self, deps: Deps,
                                            caplog: pytest.LogCaptureFixture) -> None:
        cell = h3.latlng_to_cell(51.0894, 71.4161, 9)
        secret = "мой секретный запрос про кофе"
        deps.llm = FakeLLM(fail=LLMUnavailable("quota"))
        with caplog.at_level(logging.DEBUG):
            recommend(RecommendRequest.model_validate(
                {"query": secret, "location": {"h3_cell": cell}}), deps)
        logged = "\n".join(r.getMessage() for r in caplog.records)
        assert "recommend id=" in logged
        assert secret not in logged and cell not in logged


def _core_ready(store: Store) -> bool:
    try:
        geo.haversine_m(GeoPoint(lat=51.0, lon=71.0), GeoPoint(lat=51.0, lon=71.0))
        geo.radius_for(QueryIntent(raw_query="x"))
        hours.evaluate_hours(None, hours.now_astana())
        retrieval.lexical_relevance(QueryIntent(raw_query="x"), "x")
        route.plan_route(GeoPoint(lat=51.0, lon=71.0), [], None, {})
        validate.template_cards([], QueryIntent(raw_query="x"), None)  # type: ignore[arg-type]
    except NotImplementedError:
        return False
    except Exception:  # noqa: BLE001 - implemented, just not happy with the probe arguments
        return True
    return True


def test_real_core_if_implemented(deps: Deps, monkeypatch: pytest.MonkeyPatch) -> None:
    """Becomes the integration check once the CORE lane is merged (skips until then)."""
    if not _core_ready(deps.store):
        pytest.skip("CORE stubs not implemented yet (T20–T24)")
    monkeypatch.setattr(weather, "get_weather", lambda origin, at: None)  # no network
    deps.llm = None
    a = recommend(req("Хочу тихое место с кофе и розеткой", use_llm=False), deps)
    assert a.data_used.generation == "template"
    assert len(a.cards) <= 3
    for card in a.cards:
        assert card.poi_id in deps.store.by_id
        if card.open_status == "open":
            assert card.open_confidence in {"high", "medium"}


# ------------------------------------------------- real data (DATA lane, main)

REAL_POI = Path(__file__).resolve().parent.parent / "data" / "processed" / "poi.jsonl"


@pytest.fixture(scope="module")
def real_pois() -> list[POI]:
    if not REAL_POI.exists():
        pytest.skip("data/processed/poi.jsonl not present")
    with REAL_POI.open(encoding="utf-8") as fh:
        return [POI.model_validate_json(line) for line in fh if line.strip()]


@pytest.fixture
def real_deps(real_pois: list[POI], landmarks: dict[str, Landmark], tmp_path: Path) -> Deps:
    return Deps(store=make_store(real_pois, landmarks), llm=FakeLLM(),
                settings=make_settings(tmp_path))


@pytest.mark.usefixtures("core_fakes")
class TestRealData:
    def test_s4_airport_is_not_empty(self, real_deps: Deps) -> None:
        """3 cafes and no sights near the airport: relax categories instead of failing."""
        a = recommend(RecommendRequest.model_validate({
            "query": "Я — турист, у меня 2 часа перед вылетом: что можно посмотреть поблизости?",
            "location": {"landmark_id": "airport_nqz"}, "use_llm": False}), real_deps)
        assert a.cards, a.warnings
        assert a.intent.tourist and "attraction" in a.intent.categories  # what was asked
        assert all(c.category == "cafe" for c in a.cards)  # what exists there
        assert any("Под запрошенный тип мест" in w for w in a.warnings)
        assert any("транспортного узла" in w for w in a.warnings)
        assert not any("категории:" in f for f in a.data_used.filters)  # what was applied

    def test_hours_and_wheelchair_gaps_are_counted(self, real_deps: Deps) -> None:
        a = recommend(req("кафе для коляски", use_llm=False), real_deps)
        assert a.cards
        if any(c.open_status == "unknown" for c in a.cards):
            assert any("нет данных о часах работы" in w for w in a.warnings)
        assert any(w.startswith("Доступность для коляски подтверждена для ") for w in a.warnings)

    def test_unnamed_playgrounds_are_flagged_for_the_model(self, real_deps: Deps) -> None:
        a = recommend(req("с ребёнком на площадку", use_llm=False), real_deps)
        assert a.cards and all(c.category == "playground" for c in a.cards)
        cands = fake_retrieve(real_deps.store, a.intent, a.context, None)
        assert '"name_is_generic": true' in build_prompt(a.intent, a.context, cands)

    def test_s4_tourist_deadline_from_baiterek(self, real_deps: Deps,
                                               monkeypatch: pytest.MonkeyPatch) -> None:
        """EVAL request: walk budget = 120 - 50 = 70 min -> leave by 15:10, at most 2 cards."""
        seen: list[int | None] = []
        monkeypatch.setattr(geo, "radius_for",
                            lambda intent: seen.append(intent.time_budget_min) or 1200)
        a = recommend(RecommendRequest.model_validate({
            "query": "Я — турист, у меня 2 часа перед вылетом: что можно посмотреть поблизости?",
            "location": {"landmark_id": "baiterek"}, "at": "2026-10-19T14:00:00+05:00",
            "lang": "ru", "use_llm": False}), real_deps)
        assert "Выезжайте в аэропорт не позже 15:10." in a.warnings
        assert seen == [70]  # the radius is planned on the walk budget
        assert a.intent.time_budget_min == 120  # what the user said is kept
        assert 1 <= len(a.cards) <= 2
        assert "время на прогулку 70 мин (с учётом дороги в аэропорт)" in a.data_used.filters
        assert not any("Заложите время" in w for w in a.warnings)  # replaced by the deadline
        if a.route is not None:
            assert len(a.route.stop_ids) <= 2

    def test_e11_already_at_airport(self, real_deps: Deps) -> None:
        a = recommend(RecommendRequest.model_validate({
            "query": "Я уже в аэропорту, до вылета 2 часа. Что можно посмотреть рядом?",
            "location": {"landmark_id": "airport_nqz"}, "at": "2026-10-19T14:00:00+05:00",
            "lang": "ru", "use_llm": False}), real_deps)
        assert a.summary.startswith("Рядом с аэропортом почти ничего нет; ехать в город за "
                                    "2 часа до вылета не советуем.")
        assert not any("Выезжайте" in w for w in a.warnings)  # already there
        assert all(c.category not in {"bar", "nightlife"} for c in a.cards)

    def test_e11_honest_summary_survives_llm(self, real_deps: Deps) -> None:
        a = recommend(RecommendRequest.model_validate({
            "query": "Я уже в аэропорту, до вылета 2 часа. Что можно посмотреть рядом?",
            "location": {"landmark_id": "airport_nqz"}}), real_deps)
        assert a.data_used.generation == "llm"
        assert "почти ничего нет" in a.summary

    def test_tourist_deadline_when_time_is_short(self, real_deps: Deps) -> None:
        a = recommend(req("я турист, до вылета 40 минут", at="2026-10-19T14:00:00+05:00",
                          use_llm=False), real_deps)
        assert "Выезжайте в аэропорт не позже 14:00." in a.warnings

    def test_sparse_area_warns(self, real_deps: Deps, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(geo, "radius_for", lambda intent: 150)
        a = recommend(RecommendRequest.model_validate({
            "query": "кофе", "location": {"landmark_id": "airport_nqz"}, "use_llm": False}),
            real_deps)
        assert 0 < len(a.cards) < 3
        assert any("Рядом мало мест в открытых данных" in w for w in a.warnings)
