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
import re
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
                    visit_min: dict[str, int], max_stops: int = 3, *, loop: bool = False,
                    note: str | None = None) -> MiniRoute | None:
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
                     note=note or "Маршрут приблизительный: прямые отрезки × 1,3")


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


DAYTIME = "2026-10-19T15:00:00+05:00"  # fixed: night mode switches on by the clock


def req(query: str = "кофе", **kw: Any) -> RecommendRequest:
    body: dict[str, Any] = {"query": query, "location": {"landmark_id": "expo"}, "at": DAYTIME}
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

    def test_open_now_claim_is_removed_from_llm_summary(
        self, deps: Deps, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        original = deps.llm.generate_structured

        def bragging(*, schema: type, **kw: Any) -> Any:
            out = original(schema=schema, **kw)
            if schema is DraftResponse:
                out = out.model_copy(update={"summary": "Всё открыто сейчас. Две идеи рядом."})
            return out

        monkeypatch.setattr(deps.llm, "generate_structured", bragging)
        a = recommend(req("кофе"), deps)
        assert a.data_used.generation == "llm"
        assert "открыто сейчас" not in a.summary.lower()

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

    @pytest.mark.parametrize("at", ["0001-01-01T00:00:00+14:00", "9999-12-31T23:59:59-05:00",
                                    "9999-12-31T23:59:59"])
    def test_absurd_time_is_invalid_input_before_any_llm_call(self, deps: Deps, at: str) -> None:
        with pytest.raises(InvalidRequest, match="at вне"):
            recommend(req("я турист, 2 часа", at=at), deps)
        assert deps.llm.calls == []

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
        a = recommend(req("ночью с коляской, я турист, скоро вылет", use_llm=False), deps)
        text = " | ".join(a.warnings)
        assert "Ночь:" in text and "коляски" in text and "аэропорт" in text

    def test_flight_later_is_not_a_deadline(self, deps: Deps) -> None:
        """«прогулка на 30 минут, вечером рейс»: 30 min is the walk, not the time to departure."""
        a = recommend(req("я турист, прогулка на 30 минут, вечером рейс", use_llm=False), deps)
        assert a.intent.tourist and a.intent.time_budget_min == 30 and a.cards
        assert not any("Выезжайте" in w for w in a.warnings)
        assert "Заложите время на дорогу в аэропорт и регистрацию." in a.warnings
        due = recommend(req("я турист, рейс через 3 часа, что посмотреть?", use_llm=False), deps)
        assert due.warnings[0].startswith("До вылета 180 мин")

    def test_tourist_without_flight_gets_no_airport_advice(self, deps: Deps) -> None:
        a = recommend(req("я турист, есть 2 часа", at="2026-10-19T14:00:00+05:00",
                          use_llm=False), deps)
        assert a.intent.tourist
        assert not any("аэропорт" in w for w in a.warnings)
        assert a.intent.time_budget_min == 120 and a.cards  # no −95 min without a flight

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
        now = recommend(req(use_llm=False, at=None), deps)
        assert now.context.local_time.tzinfo is not None
        assert abs(now.context.local_time - hours.now_astana()) < timedelta(minutes=1)

    def test_weather_goes_to_context_and_data_used(self, deps: Deps,
                                                   monkeypatch: pytest.MonkeyPatch) -> None:
        wx = WeatherInfo(temp_c=-3, wind_ms=5, summary="-3°C, ветер 5 м/с", outdoor_ok=True,
                         fetched_at=datetime.now(UTC))
        monkeypatch.setattr(weather, "get_weather", lambda origin, at: wx)
        a = recommend(req(use_llm=False), deps)
        assert a.context.weather == wx
        assert a.data_used.weather == "-3°C, ветер 5 м/с"
        assert "open_meteo" in a.data_used.sources
        b = recommend(req(use_llm=False, lang="en"), deps)
        assert b.data_used.weather == "-3°C, wind 5 m/s"  # same numbers, answer language
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

    @pytest.mark.parametrize(
        ("lang", "semantic", "lexical"),
        [("ru", "поиск: по смыслу и по словам", "поиск: по словам"),
         ("kk", "іздеу: мағынасы мен сөздері бойынша", "іздеу: сөздері бойынша"),
         ("en", "search: by meaning and keywords", "search: by keywords")],
    )
    def test_search_mode_is_shown(self, deps: Deps, lang: str, semantic: str,
                                  lexical: str) -> None:
        """Gemini quota can run out silently: the panel says which search actually ran."""
        deps.store.embeddings = np.zeros((len(deps.store.pois), 4), dtype=np.float32)
        deps.llm = FakeLLM(embed=np.ones(4, dtype=np.float32) / 2)
        assert semantic in recommend(req("кофе", lang=lang), deps).data_used.filters
        deps.llm = FakeLLM(embed=None)  # embed_query failed (quota) -> lexical
        filters = recommend(req("кофе", lang=lang), deps).data_used.filters
        assert lexical in filters and semantic not in filters
        no_ai = recommend(req("кофе", lang=lang, use_llm=False), deps).data_used.filters
        assert lexical in no_ai
        deps.store.embeddings = None  # no index at all
        assert lexical in recommend(req("кофе", lang=lang), deps).data_used.filters

    @pytest.mark.parametrize(
        "vec",
        [np.zeros(4, dtype=np.float32), np.ones(3, dtype=np.float32),
         np.array([np.nan, 1, 0, 0], dtype=np.float32)],
        ids=["zero", "wrong-dim", "nan"],
    )
    def test_unusable_vector_is_reported_as_lexical(
        self, deps: Deps, vec: np.ndarray, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """retrieval drops such a vector and goes lexical: the panel must say so too."""
        seen: list[Any] = []

        def spy(store: Store, intent: QueryIntent, ctx: SearchContext, qv: Any, k: int = 8
                ) -> list[Candidate]:
            seen.append(qv)
            return fake_retrieve(store, intent, ctx, qv, k)

        monkeypatch.setattr(retrieval, "retrieve", spy)
        deps.store.embeddings = np.zeros((len(deps.store.pois), 4), dtype=np.float32)
        deps.llm = FakeLLM(embed=vec)
        filters = recommend(req("кофе"), deps).data_used.filters
        assert "поиск: по словам" in filters
        assert seen and all(qv is None for qv in seen)

    def test_no_exact_match_hook(self, deps: Deps, monkeypatch: pytest.MonkeyPatch) -> None:
        """QUALITY's retrieval.no_exact_match(intent, candidates) -> warning in ru/kk/en."""
        monkeypatch.setattr(retrieval, "no_exact_match", lambda intent, cands: True,
                            raising=False)
        texts = {"ru": "Точного совпадения по запросу нет — показано, что есть рядом.",
                 "kk": "Сұрауға дәл сәйкес орын жоқ — жақын маңдағы орындар көрсетілді.",
                 "en": "No exact match for your request — showing what is nearby."}
        for lang, text in texts.items():
            assert text in recommend(req("кофе", lang=lang, use_llm=False), deps).warnings
        monkeypatch.setattr(retrieval, "no_exact_match", lambda intent, cands: False)
        assert not any("совпадения" in w
                       for w in recommend(req("кофе", use_llm=False), deps).warnings)

    def test_no_exact_match_hook_absent_or_broken(self, deps: Deps,
                                                  monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delattr(retrieval, "no_exact_match", raising=False)
        assert recommend(req("кофе", use_llm=False), deps).cards

        def boom(*_a: Any) -> bool:
            raise RuntimeError("bug")

        monkeypatch.setattr(retrieval, "no_exact_match", boom, raising=False)
        assert recommend(req("кофе", use_llm=False), deps).cards  # never kills the answer

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
        """Walk budget = 120 − 35 transfer − 60 reserve = 25 min -> leave by 14:25, ≤ 2 cards."""
        seen: list[tuple[int | None, int | None]] = []
        monkeypatch.setattr(geo, "radius_for", lambda intent: seen.append(
            (intent.time_budget_min, intent.max_walk_min)) or 865)
        a = recommend(RecommendRequest.model_validate({
            "query": "Я — турист, у меня 2 часа перед вылетом: что можно посмотреть поблизости?",
            "location": {"landmark_id": "baiterek"}, "at": "2026-10-19T14:00:00+05:00",
            "lang": "ru", "use_llm": False}), real_deps)
        assert ("До вылета 120 мин: дорога до аэропорта ≈35 мин (оценка) и запас 60 мин — "
                "на прогулку 25 мин. Выезжайте в аэропорт не позже 14:25.") in a.warnings
        assert seen == [(25, 15)]  # planned on the walk budget, tourist walks ≤ 15 min
        assert a.intent.time_budget_min == 120  # what the user said is kept
        assert 1 <= len(a.cards) <= 2
        assert all(c.walk_min <= 15 for c in a.cards)
        assert "время на прогулку 25 мин (120 − 35 дорога − 60 запас)" in a.data_used.filters
        assert not any("Заложите время" in w for w in a.warnings)  # replaced by the plan
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

    def test_no_time_before_flight_means_no_cards(self, real_deps: Deps) -> None:
        """110 − 95 = 15 is the minimum; 100 − 95 = 5 min -> no cards, "leave now"."""
        a = recommend(req("я турист, до вылета 100 минут", at="2026-10-19T14:00:00+05:00"),
                      real_deps)
        assert a.cards == [] and a.route is None
        assert a.summary == "Времени на прогулку перед вылетом нет — выезжайте в аэропорт сейчас."
        assert any(w.endswith("Выезжайте в аэропорт сейчас.") and "≈35 мин (оценка)" in w
                   for w in a.warnings)
        assert not any("Подходящих мест не нашлось" in w for w in a.warnings)
        assert real_deps.llm.calls == ["QueryIntent"]  # no generation was paid for
        enough = recommend(req("я турист, до вылета 110 минут", at="2026-10-19T14:00:00+05:00",
                               use_llm=False), real_deps)
        assert enough.cards and any("не позже 14:15" in w for w in enough.warnings)

    def test_departure_override_is_a_flight(self, real_deps: Deps) -> None:
        a = recommend(req("что посмотреть рядом?", at="2026-10-19T14:00:00+05:00",
                          use_llm=False, overrides={"departure_in_min": 150}), real_deps)
        assert a.intent.tourist and a.intent.time_budget_min == 150
        assert any("на прогулку 55 мин" in w and "не позже 14:55" in w for w in a.warnings)

    def test_hub_and_tourist_walks_are_short_without_widening(
        self, real_deps: Deps, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """E11: no ×1.5 radius widening at a hub; candidates beyond 15 min are dropped."""
        radii: list[int] = []
        real_retrieve = fake_retrieve

        def spy(store: Store, intent: QueryIntent, ctx: SearchContext, qv: Any, k: int = 8
                ) -> list[Candidate]:
            radii.append(ctx.radius_m)
            out = real_retrieve(store, intent, ctx, qv, k)
            far = out[0].model_copy(update={"walk_min": 48}) if out else None
            return out + ([far] if far else [])

        monkeypatch.setattr(retrieval, "retrieve", spy)
        a = recommend(RecommendRequest.model_validate({
            "query": "Я уже в аэропорту, до вылета 2 часа. Что можно посмотреть рядом?",
            "location": {"landmark_id": "airport_nqz"}, "use_llm": False}), real_deps)
        assert a.intent.max_walk_min is None and a.context.radius_m == radii[0]
        assert len(set(radii)) == 1  # never widened
        assert all(c.walk_min <= 15 for c in a.cards)

    def test_sparse_area_warns(self, real_deps: Deps, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(geo, "radius_for", lambda intent: 150)
        a = recommend(RecommendRequest.model_validate({
            "query": "кофе", "location": {"landmark_id": "airport_nqz"}, "use_llm": False}),
            real_deps)
        assert 0 < len(a.cards) < 3
        assert any("Рядом мало мест в открытых данных" in w for w in a.warnings)


# ------------------------------------- real CORE + real data (wave 3, deterministic)


def real_req(query: str, landmark: str, **kw: Any) -> RecommendRequest:
    body: dict[str, Any] = {"query": query, "location": {"landmark_id": landmark},
                            "at": "2026-10-19T15:00:00+05:00", "use_llm": False}
    body.update(kw)
    return RecommendRequest.model_validate(body)


@pytest.fixture
def no_weather(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(weather, "get_weather", lambda origin, at: None)


@pytest.mark.usefixtures("no_weather")
class TestRealCore:
    def test_s1_walk_is_a_loop_within_budget(self, real_deps: Deps) -> None:
        a = recommend(real_req("Я сейчас возле Mega Silk Way, хочу прогуляться 30–60 минут — "
                               "что посоветуете?", "mega_silk_way"), real_deps)
        assert 1 <= len(a.cards) <= 3
        r = a.route
        assert r is not None, a.warnings
        assert r.legs[-1].to_id == "origin" and r.path[0] == r.path[-1]
        assert r.fits_budget and r.total_min <= 60
        assert set(r.stop_ids) <= {c.poi_id for c in a.cards}
        assert "с возвращением к старту" in r.note

    def test_p1_walk_starts_with_a_place_to_walk(self, real_deps: Deps) -> None:
        """Probe P1: at Mega Silk Way «прогуляться 30–60 минут» -> a park first, not the mall."""
        a = recommend(real_req("Я сейчас возле Mega Silk Way, хочу прогуляться 30–60 минут — "
                               "что посоветуете?", "mega_silk_way"), real_deps)
        assert a.cards[0].category in {"park", "viewpoint", "monument", "attraction"}
        assert all(c.distance_m >= 100 for c in a.cards)  # not the mall you stand at
        r = a.route
        assert r is not None and r.fits_budget and r.total_min <= 60
        names = {c.poi_id: c.title for c in a.cards}
        in_route = [names.get(pid, "") for pid in r.stop_ids]
        assert "NU Park" in in_route or a.cards[0].title == "NU Park"
        assert not any("Mega Silk Way" in n for n in in_route)
        # S1: alternatives of a walk are walk stops too (no restaurant / fast food / bar)
        assert not {c.category for c in a.cards} & {"restaurant", "fast_food", "bar"}

    def test_walk_with_kids_note_asks_for_extra_time(self, real_deps: Deps) -> None:
        a = recommend(real_req("Хочу погулять с ребёнком часик", "khan_shatyr"), real_deps)
        if a.route is not None:
            assert "С ребёнком закладывайте больше времени." in a.route.note

    def test_night_by_the_clock(self, real_deps: Deps) -> None:
        a = recommend(real_req("кофе", "khan_shatyr", at="2026-10-19T23:30:00+05:00"),
                      real_deps)
        assert not a.intent.night  # nothing about the night in the text...
        assert "ночь (по времени)" in a.data_used.filters  # ...but it is 23:30
        assert any("обратно лучше на такси" in w for w in a.warnings)
        night_tags = {"late_open", "24_7", "night"}
        for card in a.cards:
            assert card.open_status == "open" or night_tags & set(card.tags), card.title
            assert card.walk_min <= 15
        day = recommend(real_req("кофе", "khan_shatyr"), real_deps)
        assert not any("ночь" in f for f in day.data_used.filters)

    def test_e2_deep_night_prefers_surely_open_places(self, real_deps: Deps) -> None:
        """02:30: unknown-hours bars 10 min away lose to open/24_7 places further away."""
        a = recommend(real_req("Сейчас глубокая ночь, куда можно пойти поесть или посидеть?",
                               "khan_shatyr", at="2026-10-20T02:30:00+05:00", lang="ru"),
                      real_deps)
        assert a.cards
        for card in a.cards:
            assert card.open_status == "open" or {"late_open", "24_7"} & set(card.tags)
        assert any("лучше доехать на такси" in w or "обратно лучше на такси" in w
                   for w in a.warnings)

    def test_night_switched_off_in_ui(self, real_deps: Deps) -> None:
        a = recommend(real_req("кофе", "khan_shatyr", at="2026-10-19T23:30:00+05:00",
                               overrides={"night": False}), real_deps)
        assert not any("ночь" in f for f in a.data_used.filters)

    def test_kids_walk_short_unless_user_says_otherwise(self, real_deps: Deps) -> None:
        a = recommend(real_req("куда сходить с ребёнком", "khan_shatyr"), real_deps)
        assert "пешком до 15 мин" in a.data_used.filters
        assert all(c.walk_min <= 15 for c in a.cards)
        own = recommend(real_req("с ребёнком, не дальше 25 минут пешком", "khan_shatyr"),
                        real_deps)
        assert "пешком до 25 мин" in own.data_used.filters

    def test_kids_at_night_warning(self, real_deps: Deps) -> None:
        a = recommend(real_req("с ребёнком", "khan_shatyr", at="2026-10-19T23:00:00+05:00"),
                      real_deps)
        assert any("поздно для прогулки с ребёнком" in w for w in a.warnings)

    def test_group_warning_for_food_places(self, real_deps: Deps) -> None:
        a = recommend(real_req("нас 5 человек, хотим поесть", "khan_shatyr"), real_deps)
        assert a.intent.group_size == 5 and a.cards
        assert "Для компании из 5 человек: столик в кафе лучше уточнить заранее." in a.warnings

    def test_wheelchair_note_and_short_walks(self, real_deps: Deps) -> None:
        a = recommend(real_req("Я на коляске, хочу прогуляться час", "khan_shatyr"), real_deps)
        assert "пешком до 15 мин" in a.data_used.filters
        if a.route is not None:
            assert "ступени" in a.route.note and "закладывайте больше времени" in a.route.note

    def test_e7_english_answer_has_no_russian_text(self, real_deps: Deps) -> None:
        a = recommend(real_req("I'm near EXPO with one free hour. What's worth seeing nearby?",
                               "expo"), real_deps)
        assert a.lang == "en" and 1 <= len(a.cards) <= 3
        assert a.intent.time_budget_min == 60 and "free" not in a.intent.must_tags
        texts = [a.summary, a.disclaimer, a.context.area_label, a.data_used.location,
                 a.data_used.time, a.data_used.weather or "", *a.warnings, *a.data_used.filters]
        texts += [t for c in a.cards for t in (c.why, c.what_to_do, c.open_note)]
        if a.route is not None:
            texts.append(a.route.note)
        cyrillic = [x for x in texts if re.search(r"[А-Яа-яЁё]", x)]
        assert cyrillic == []  # only POI titles / addresses may stay in the local script
        assert a.context.area_label.startswith("near EXPO")

    def test_e6_kazakh_answer_has_no_russian_service_text(self, real_deps: Deps) -> None:
        a = recommend(real_req("Мен Бәйтерек жанындамын, бір сағатқа серуендегім келеді. "
                               "Не ұсынасыз?", "baiterek"), real_deps)
        assert a.lang == "kk" and 1 <= len(a.cards) <= 3
        russian = re.compile(r"нет данных|по данным|ежедневно|проверьте|закроется|откроется|"
                             r"погода|радиус поиска|пешком|время|у Монумент")
        texts = [a.summary, a.disclaimer, a.context.area_label, a.data_used.location,
                 a.data_used.weather or "", *a.warnings, *a.data_used.filters]
        texts += [t for c in a.cards for t in (c.why, c.what_to_do, c.open_note)]
        if a.route is not None:
            texts.append(a.route.note)
        assert [x for x in texts if russian.search(x)] == []
        assert a.context.area_label.endswith("маңында")
        assert "серуен" in " ".join(a.data_used.filters)  # tag name, not the code "walk"

    def test_kazakh_cell_area_label(self, real_deps: Deps) -> None:
        cell = h3.latlng_to_cell(51.128, 71.430, 9)
        a = recommend(RecommendRequest.model_validate({
            "query": "кофе", "location": {"h3_cell": cell}, "lang": "kk", "at": DAYTIME,
            "use_llm": False}), real_deps)
        assert a.context.area_label.startswith("сіздің ауданыңыз (≈0,1 км²)")

    def test_ui_tourist_switch_at_the_airport_is_honest(self, real_deps: Deps) -> None:
        """POLISH: tourist + departure 120 from the UI, text without categories -> 2 cafes are
        found, and the summary still says there is nothing to see near NQZ."""
        a = recommend(real_req("Что рядом?", "airport_nqz", lang="ru",
                               overrides={"tourist": True, "departure_in_min": 120}), real_deps)
        assert a.intent.tourist and a.cards
        assert a.summary.startswith("Рядом с аэропортом почти ничего нет; ехать в город за "
                                    "2 часа до вылета не советуем.")
        coffee = recommend(real_req("где кофе?", "airport_nqz", lang="ru",
                                    overrides={"tourist": True}), real_deps)
        assert "почти ничего нет" not in coffee.summary  # food was asked: no lecture

    def test_airport_deadline_is_the_first_warning(self, real_deps: Deps) -> None:
        a = recommend(real_req("Я — турист, у меня 2 часа перед вылетом: что посмотреть?",
                               "baiterek", lang="ru"), real_deps)
        assert a.warnings[0].startswith("До вылета 120 мин")

    def test_ui_night_and_tourist_switches(self, real_deps: Deps) -> None:
        s5 = recommend(real_req("куда сходить?", "khan_shatyr", overrides={"night": True}),
                       real_deps)
        assert s5.intent.night and any(w.startswith("Ночь:") for w in s5.warnings)
        s4 = recommend(real_req("что посмотреть?", "baiterek", lang="ru",
                                overrides={"tourist": True, "departure_in_min": 120}),
                       real_deps)
        assert s4.intent.tourist and s4.intent.time_budget_min == 120
        assert s4.warnings[0].startswith("До вылета 120 мин") and len(s4.cards) <= 2

    def test_group_collective_numerals_reach_the_warning(self, real_deps: Deps) -> None:
        a = recommend(real_req("нас пятеро, хотим поесть", "khan_shatyr"), real_deps)
        assert a.intent.group_size == 5
        assert "Для компании из 5 человек: столик в кафе лучше уточнить заранее." in a.warnings

    def test_no_route_at_a_transport_hub(self, real_deps: Deps) -> None:
        a = recommend(real_req("Я уже в аэропорту, до вылета 2 часа. Что можно посмотреть "
                               "рядом?", "airport_nqz", lang="ru"), real_deps)
        assert a.cards and a.route is None
        assert all(c.walk_min <= 15 for c in a.cards)

    def test_tourist_route_is_one_way(self, real_deps: Deps) -> None:
        a = recommend(real_req("Я турист, у меня 3 часа до вылета, хочу прогуляться",
                               "baiterek"), real_deps)
        assert len(a.cards) <= 2
        if a.route is not None:
            assert a.route.legs[-1].to_id != "origin"
            assert "без обратной дороги" in a.route.note
