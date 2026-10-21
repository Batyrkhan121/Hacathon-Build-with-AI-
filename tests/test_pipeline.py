"""app/pipeline.py end to end with CORE fakes and a fake LLM (no network).

CORE modules (geo, retrieval, validate, route) belong to another lane: the fixture
`core_fakes` swaps them for small deterministic fakes that follow the stub signatures.
`test_real_core_if_implemented` runs the same flow on the real modules once they exist.
test_api.py imports the helpers below (FakeLLM, make_store, install_core_fakes, ...).
"""

from __future__ import annotations

import dataclasses
import json
import logging
import math
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import h3
import numpy as np
import pytest
from pydantic import ValidationError

from app import geo, hours, retrieval, route, validate, weather
from app import pipeline as pipeline_mod
from app.config import Settings, get_settings
from app.generate import build_prompt
from app.llm import LLMUnavailable
from app.pipeline import TEXTS, Deps, InvalidRequest, UnknownLandmarkError, recommend
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

# the honest tail of an event note (app.events)
CHECK_RU = " — по данным источника, проверьте перед визитом."
CHECK_KK = " — дереккөз бойынша, барар алдында тексеріңіз."
CHECK_EN = " — per the source, check before you go."

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
                    note: str | None = None, lang: str = "ru", profile: str = "fastest"
                    ) -> MiniRoute | None:
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
    """Duck-typed LLMClient: answers QueryIntent / DraftResponse, records calls.

    Like the real client: unavailable (cooldown / no key) -> LLMUnavailable at once, with the
    reason in `last_error`; `parse_fail` breaks only the QueryIntent call."""

    def __init__(self, *, available: bool = True, fail: Exception | None = None,
                 intent: QueryIntent | None = None, embed: np.ndarray | None = None,
                 error_kind: str = "network", parse_fail: Exception | None = None,
                 embed_status: str | None = None, enabled: bool = True,
                 summary: str = "Идеи от модели", whys: list[str] | None = None) -> None:
        self.available = available
        self.summary = summary
        self.whys = whys or []
        self.enabled = enabled
        self.fail = fail
        self.parse_fail = parse_fail
        self.intent = intent
        self.embed = embed
        self.error_kind = error_kind
        self.embed_status = embed_status
        self.model_name = "fake-claude"
        self.last_model: str | None = None
        self.last_error: str | None = None
        self.last_embed_status: str | None = None
        self.calls: list[str] = []
        self.embedded: list[str] = []

    def generate_structured(self, *, system: str, prompt: str, schema: type, effort: str = "low"
                            ) -> Any:
        self.calls.append(schema.__name__)
        self.last_error = None
        failure = self.parse_fail if schema is QueryIntent and self.parse_fail else self.fail
        if not self.available:
            failure = LLMUnavailable("cooling down")
        if failure is not None:
            if isinstance(failure, LLMUnavailable):
                self.last_error = self.error_kind
            raise failure
        self.last_model = "fake-claude"
        if schema is QueryIntent:
            return self.intent or QueryIntent(raw_query="x", parser="llm")
        ids = [line.split('"')[3] for line in prompt.splitlines() if line.strip()
               .startswith('"id":')]
        return DraftResponse(summary=self.summary, cards=[
            CardDraft(poi_id=i, why=self.whys[n] if n < len(self.whys) else "Рядом и подходит.",
                      what_to_do="Загляните на 20 минут.",
                      visit_min_low=20, visit_min_high=30, idea_source="generated")
            for n, i in enumerate(ids[:2])])

    def embed_query(self, text: str) -> np.ndarray | None:
        self.embedded.append(text)
        self.last_embed_status = self.embed_status or ("ok" if self.embed is not None
                                                       else "error")
        return self.embed


def make_settings(tmp_path: Path) -> Settings:
    # index_dir too: the real data/index/meta.json may say "stale" — tests must not care
    # no embeddings keys either: a test that needs one sets it explicitly (.env must not matter)
    return dataclasses.replace(get_settings(), cache_dir=tmp_path / "cache",
                               index_dir=tmp_path / "index", h3_res=9,
                               embed_provider="gemini", embed_model="gemini-embedding-001",
                               gemini_api_key=None, openrouter_api_key=None)


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
        assert "LLM недоступна (нет связи) — показаны карточки по данным." in a.warnings
        assert a.cards

    def test_unavailable_llm_warns_once(self, deps: Deps) -> None:
        deps.llm = FakeLLM(available=False)
        a = recommend(req(), deps)
        assert a.data_used.generation == "template"
        assert sum(w.startswith("LLM недоступна") for w in a.warnings) == 1
        assert not any(w.startswith("Запрос разобран по правилам") for w in a.warnings)

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

    def test_cell_finer_than_r10_is_rejected_by_the_contract(self, deps: Deps) -> None:
        fine = h3.latlng_to_cell(51.0894, 71.4161, 13)  # ~40 m²: effectively a raw coordinate
        with pytest.raises(ValidationError, match="finer than r10"):
            RecommendRequest.model_validate(
                {"query": "кофе", "location": {"h3_cell": fine}, "use_llm": False})

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
        assert ("Поиск дал сбой на нашей стороне — повтор не поможет. Попробуйте другой запрос "
                "или точку.") in a.warnings

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
        assert recommend(req(use_llm=False, at=None),
                         deps).data_used.weather == "погода неизвестна"

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
        deps.llm = FakeLLM(embed=None)  # embed_query failed -> lexical, with the reason
        filters = recommend(req("кофе", lang=lang), deps).data_used.filters
        assert any(f.startswith(lexical + " (") for f in filters) and semantic not in filters
        no_ai = recommend(req("кофе", lang=lang, use_llm=False), deps).data_used.filters
        assert lexical in no_ai  # the user chose it: no reason to explain
        deps.store.embeddings = None  # no index at all
        no_index = recommend(req("кофе", lang=lang), deps).data_used.filters
        assert any(f.startswith(lexical) for f in no_index)  # (+ «отбор — Claude» with AI)

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
        assert "поиск: по словам (некорректный вектор запроса), отбор — Claude" in filters
        assert seen and all(qv is None for qv in seen)

    def test_query_unmatched_hook(self, deps: Deps, monkeypatch: pytest.MonkeyPatch) -> None:
        """QUALITY's retrieval.query_unmatched(store, intent, ctx, candidates) -> warning."""
        seen: list[tuple[Any, ...]] = []

        def unmatched(store: Store, intent: QueryIntent, ctx: SearchContext,
                      cands: list[Candidate]) -> bool:
            seen.append((store, intent, ctx, cands))
            return True

        monkeypatch.setattr(retrieval, "query_unmatched", unmatched, raising=False)
        texts = {"ru": "Точного совпадения по запросу нет — показано, что есть рядом.",
                 "kk": "Сұрауға дәл сәйкес орын жоқ — жақын маңдағы орындар көрсетілді.",
                 "en": "No exact match for your request — showing what is nearby."}
        for lang, text in texts.items():
            assert text in recommend(req("кофе", lang=lang, use_llm=False), deps).warnings
        store, _intent, ctx, cands = seen[0]
        assert store is deps.store and isinstance(ctx, SearchContext) and cands
        monkeypatch.setattr(retrieval, "query_unmatched", lambda *a: False)
        assert not any("совпадения" in w
                       for w in recommend(req("кофе", use_llm=False), deps).warnings)

    def test_query_unmatched_absent_or_broken(self, deps: Deps,
                                              monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delattr(retrieval, "query_unmatched", raising=False)
        assert recommend(req("кофе", use_llm=False), deps).cards  # old main: no function yet

        def boom(*_a: Any) -> bool:
            raise RuntimeError("bug")

        monkeypatch.setattr(retrieval, "query_unmatched", boom, raising=False)
        assert recommend(req("кофе", use_llm=False), deps).cards  # never kills the answer

    def test_llm_summary_is_sanitized_as_llm(self, deps: Deps,
                                             monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[bool] = []

        def sanitize(summary: str, cards: list[Card], llm: bool = False) -> str:
            calls.append(llm)
            return summary

        monkeypatch.setattr(validate, "sanitize_summary", sanitize)
        assert recommend(req("кофе"), deps).data_used.generation == "llm"
        assert calls == [True]  # the LLM wrote it
        recommend(req("кофе", use_llm=False), deps)
        assert calls == [True, False]  # template counters / airport times: default

    def test_stale_index_is_named_in_the_search_line(self, deps: Deps, tmp_path: Path) -> None:
        import json

        index = tmp_path / "index"
        index.mkdir()
        (index / "meta.json").write_text(json.dumps({"status": "stale"}), encoding="utf-8")
        deps.settings = dataclasses.replace(deps.settings, index_dir=index)
        deps.store.embeddings = np.zeros((len(deps.store.pois), 4), dtype=np.float32)
        deps.llm = FakeLLM(embed=np.ones(4, dtype=np.float32) / 2)
        filters = recommend(req("кофе"), deps).data_used.filters
        assert "поиск: по смыслу и по словам (индекс устарел)" in filters
        en = recommend(req("coffee", lang="en"), deps).data_used.filters
        assert "search: by meaning and keywords (index outdated)" in en

    @pytest.mark.parametrize(
        ("lang", "line", "warning"),
        [("ru", "поиск: по словам (индекс не подходит к модели запросов), отбор — Claude",
          "Смысловой поиск недоступен: индекс построен другой моделью — поиск только по словам."),
         ("kk", "іздеу: сөздері бойынша (индекс сұрау моделіне сәйкес емес), іріктеу — Claude",
          "Мағыналық іздеу қолжетімсіз: индекс басқа модельмен құрылған — тек сөздер бойынша "
          "іздеу."),
         ("en", "search: by keywords (index does not match the query model), picked by Claude",
          "Semantic search is unavailable: the index was built with another model — keyword "
          "search only.")],
    )
    def test_index_of_another_model_is_named(self, deps: Deps, tmp_path: Path, lang: str,
                                             line: str, warning: str) -> None:
        """EMBED_PROVIDER=openrouter over an index built with gemini-embedding-001: Store drops
        the vectors; the answer must say why instead of a bare «по словам»."""
        import json

        index = tmp_path / "index"
        index.mkdir()
        (index / "meta.json").write_text(json.dumps(
            {"model": "gemini-embedding-001", "dim": 768, "status": "ok"}), encoding="utf-8")
        deps.settings = dataclasses.replace(
            deps.settings, index_dir=index, embed_provider="openrouter",
            openrouter_api_key="or-key", embed_model="google/gemini-embedding-001", embed_dim=768)
        deps.store.embeddings = None  # dropped by Store: model mismatch
        a = recommend(req(lang=lang), deps)
        assert line in a.data_used.filters
        assert warning in a.warnings
        b = recommend(req(lang=lang, use_llm=False), deps)  # the user chose no AI
        assert not any("(" in f for f in b.data_used.filters if "Claude" in f or "по словам" in f)

    def test_missing_index_with_a_key_is_named(self, deps: Deps, tmp_path: Path) -> None:
        deps.settings = dataclasses.replace(deps.settings, index_dir=tmp_path / "none",
                                            gemini_api_key="g-key")
        deps.store.embeddings = None
        a = recommend(req(), deps)
        assert "поиск: по словам (индекс не загружен), отбор — Claude" in a.data_used.filters
        assert ("Смысловой поиск недоступен: индекс эмбеддингов не загружен — поиск только по "
                "словам.") in a.warnings

    def test_no_key_and_no_index_stays_quiet(self, deps: Deps) -> None:
        deps.settings = dataclasses.replace(deps.settings, gemini_api_key=None)
        deps.store.embeddings = None
        a = recommend(req(), deps)
        assert "поиск: по словам, отбор — Claude" in a.data_used.filters
        assert not any("Смысловой" in w for w in a.warnings)

    def test_hours_dropped_in_filters(self, deps: Deps, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(retrieval, "hours_dropped", lambda store, intent, ctx: {
            "closed": 3, "closed_partial": 1, "closing_soon": 2, "night": 0}, raising=False)
        ru = recommend(req("кофе", use_llm=False), deps).data_used.filters
        assert ("по часам работы отсеяно 6: закрыто 3, сегодня не работает 1, "
                "скоро закроется 2") in ru
        kk = recommend(req("кофе", lang="kk", use_llm=False), deps).data_used.filters
        assert any(f.startswith("жұмыс уақыты бойынша алынып тасталды 6") for f in kk)
        en = recommend(req("coffee", lang="en", use_llm=False), deps).data_used.filters
        assert any(f.startswith("removed by opening hours 6: closed 3") for f in en)
        monkeypatch.setattr(retrieval, "hours_dropped", lambda *a: {"closed": 0})
        assert not any("отсеяно" in f
                       for f in recommend(req("кофе", use_llm=False), deps).data_used.filters)

        def boom(*_a: Any) -> dict[str, int]:
            raise RuntimeError("bug")

        monkeypatch.setattr(retrieval, "hours_dropped", boom)
        assert recommend(req("кофе", use_llm=False), deps).cards  # never kills the answer

    def test_old_sanitize_signature_still_works(self, deps: Deps,
                                                monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(validate, "sanitize_summary", lambda summary, cards: summary)
        assert recommend(req("кофе"), deps).summary == "Идеи от модели"

    # ----------------------------------- raw tags in the model summary (S5 live)

    @pytest.mark.parametrize(("lang", "leak", "raw"), [
        ("ru", "Поздним вечером: все с тегом night рядом с EXPO.", ("night", "тег")),
        ("kk", "Кешке қарай late_open орындары жақын.", ("late_open",)),
    ])
    def test_model_summary_with_a_raw_tag_becomes_the_template(
            self, deps: Deps, lang: str, leak: str, raw: tuple[str, ...]) -> None:
        deps.llm = FakeLLM(summary=leak)
        a = recommend(req("кофе", lang=lang), deps)
        assert a.lang == lang and a.data_used.generation == "llm" and a.cards
        assert a.summary == pipeline_mod._template_summary(pipeline_mod.TEXTS[lang],
                                                           len(a.cards))
        assert not any(word in a.summary for word in raw)

    def test_english_night_is_a_word_not_a_tag(self, deps: Deps) -> None:
        deps.llm = FakeLLM(summary="A quiet night walk by the river.")
        a = recommend(req("coffee", lang="en"), deps)
        assert a.lang == "en" and a.summary == "A quiet night walk by the river."

    def test_template_summary_is_not_checked_for_tags(self, deps: Deps,
                                                      monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(pipeline_mod, "_template_summary", lambda t, n: "Ночью: тег night")
        assert recommend(req("кофе", use_llm=False), deps).summary == "Ночью: тег night"

    # ----------------------------------- raw tags in a model-written why (cache: 6 of 66)

    @pytest.mark.parametrize("leak", ["Рядом, есть кофе и wifi.",
                                      "Площадка отмечена тегом «для детей»."])
    def test_model_why_with_a_raw_tag_becomes_the_template_why(self, deps: Deps,
                                                                leak: str) -> None:
        deps.llm = FakeLLM(whys=[leak, "Тихо и уютно."])
        a = recommend(req("кофе"), deps)
        assert a.data_used.generation == "llm" and len(a.cards) == 2
        first, second = a.cards
        assert first.why == f"≈{first.walk_min} мин пешком"  # fake_template_cards: matched
        assert first.what_to_do == "Загляните на 20 минут." and first.idea_source == "generated"
        assert second.why == "Тихо и уютно."  # the other card is not touched

    def test_english_wifi_in_a_why_is_a_word(self, deps: Deps) -> None:
        deps.llm = FakeLLM(whys=["Quiet place with wifi and coffee."])
        a = recommend(req("coffee", lang="en"), deps)
        assert a.lang == "en" and a.cards[0].why == "Quiet place with wifi and coffee."

    def test_template_cards_are_not_checked_for_tags(self, deps: Deps,
                                                     monkeypatch: pytest.MonkeyPatch) -> None:
        def tagged(candidates: list[Candidate], intent: QueryIntent, ctx: SearchContext,
                   n: int = 3) -> list[Card]:
            return [c.model_copy(update={"why": "Есть wifi."})
                    for c in fake_template_cards(candidates, intent, ctx, n)]

        monkeypatch.setattr(validate, "template_cards", tagged)
        a = recommend(req("кофе", use_llm=False), deps)
        assert a.data_used.generation == "template" and a.cards
        assert all(c.why == "Есть wifi." for c in a.cards)

    @pytest.mark.parametrize("lang", ["ru", "kk", "en"])
    def test_template_reasons_never_look_like_raw_tags(self, lang: str) -> None:
        # every phrase a template why is made of (retrieval.REASONS, MISSING_TAG_WORDS)
        what = ", ".join(w.get(lang, w["ru"]) for w in retrieval.MISSING_TAG_WORDS.values())
        for key, texts in retrieval.REASONS.items():
            for src in retrieval.SOURCE_LABELS.values():
                text = texts.get(lang, texts["ru"]).format(n=7, src=src, what=what)
                assert not pipeline_mod._tag_leak(text, lang, []), (key, text)

    @pytest.mark.parametrize(("lang", "text", "titles", "leak"), [
        ("ru", "Поздним вечером: все с тегом night", [], True),
        ("ru", "Здесь есть wifi и розетки", [], True),
        ("ru", "Места с тегами для детей", [], True),
        ("ru", "Работать удобно: power_outlets", [], True),
        ("ru", "Кофейня 24_7 рядом", [], True),
        ("ru", "Тихая прогулка вечером вдоль воды", [], False),
        ("ru", "Кофе в зоне Wi-Fi", [], False),
        ("ru", "Загляните в Kids Park", ["Kids Park"], False),
        ("kk", "Кешке қарай late_open орындары", [], True),
        ("kk", "Бұл орынның тегі бар", [], True),
        ("kk", "Балаларға арналған kids орын", [], True),
        ("kk", "Кешкі серуен", [], False),
        ("en", "A quiet night walk by the river", [], False),
        ("en", "Kids will love the view", [], False),
        ("en", "Cafes with power_outlets nearby", [], True),
        ("en", "Places tagged night are close", [], True),
        ("en", "Everything with the kids tag", [], True),
    ])
    def test_tag_leak_detection(self, lang: str, text: str, titles: list[str],
                                leak: bool) -> None:
        assert pipeline_mod._tag_leak(text, lang, titles) is leak

    # ----------------------------------- no query vector: Claude picks from 16

    @pytest.mark.parametrize(
        ("lang", "picked", "lexical", "semantic"),
        [("ru", "поиск: по словам, отбор — Claude", "поиск: по словам",
          "поиск: по смыслу и по словам"),
         ("kk", "іздеу: сөздері бойынша, іріктеу — Claude", "іздеу: сөздері бойынша",
          "іздеу: мағынасы мен сөздері бойынша"),
         ("en", "search: by keywords, picked by Claude", "search: by keywords",
          "search: by meaning and keywords")],
    )
    def test_claude_picks_from_a_wider_keyword_pool(
        self, deps: Deps, monkeypatch: pytest.MonkeyPatch, lang: str, picked: str,
        lexical: str, semantic: str
    ) -> None:
        ks: list[int] = []

        def spy(store: Store, intent: QueryIntent, ctx: SearchContext, qv: Any, k: int = 8
                ) -> list[Candidate]:
            ks.append(k)
            return fake_retrieve(store, intent, ctx, qv, k)

        monkeypatch.setattr(retrieval, "retrieve", spy)
        # 1) no vector, Claude on -> 16 keyword candidates, Claude picks
        a = recommend(req("кофе", lang=lang), deps)
        assert ks[-1] == 16 and a.data_used.generation == "llm"
        assert picked in a.data_used.filters
        # 2) no AI -> 8 candidates, plain keyword search
        b = recommend(req("кофе", lang=lang, use_llm=False), deps)
        assert ks[-1] == 8 and lexical in b.data_used.filters
        assert not any("Claude" in f for f in b.data_used.filters)
        # 3) a usable vector -> 8 candidates, semantic search
        deps.store.embeddings = np.zeros((len(deps.store.pois), 4), dtype=np.float32)
        deps.llm = FakeLLM(embed=np.ones(4, dtype=np.float32) / 2)
        c = recommend(req("кофе", lang=lang), deps)
        assert ks[-1] == 8 and semantic in c.data_used.filters

    def test_claude_unavailable_keeps_the_usual_pool(self, deps: Deps,
                                                     monkeypatch: pytest.MonkeyPatch) -> None:
        ks: list[int] = []

        def spy(store: Store, intent: QueryIntent, ctx: SearchContext, qv: Any, k: int = 8
                ) -> list[Candidate]:
            ks.append(k)
            return fake_retrieve(store, intent, ctx, qv, k)

        monkeypatch.setattr(retrieval, "retrieve", spy)
        deps.llm = FakeLLM(available=False)
        a = recommend(req("кофе"), deps)
        assert ks == [8] and "поиск: по словам" in a.data_used.filters
        deps.llm = FakeLLM(fail=LLMUnavailable("timeout"))  # asked, then failed
        b = recommend(req("кофе"), deps)
        assert ks[-1] == 16 and b.data_used.generation == "template"
        assert "поиск: по словам" in b.data_used.filters  # no «отбор — Claude» claim
        assert len(b.cards) <= 3

    # ------------------------------------------- silent-failure review (wave 3)

    def test_llm_down_says_why(self, deps: Deps) -> None:
        deps.llm = FakeLLM(fail=LLMUnavailable("400"), error_kind="no_credit")
        a = recommend(req(), deps)
        assert "LLM недоступна (закончились кредиты API) — показаны карточки по данным." in (
            a.warnings)
        assert not any(w.startswith("Запрос разобран по правилам") for w in a.warnings)

    def test_cooling_llm_is_still_asked_for_its_cache(self, deps: Deps) -> None:
        deps.llm = FakeLLM(available=False, error_kind="quota")
        a = recommend(req(), deps)
        assert deps.llm.calls  # the client answers from cache or fails fast by itself
        assert "LLM недоступна (лимит запросов) — показаны карточки по данным." in a.warnings

    def test_switched_off_llm_is_never_called(self, deps: Deps) -> None:
        deps.llm = FakeLLM(enabled=False)
        a = recommend(req(), deps)
        assert deps.llm.calls == [] and a.data_used.generation == "template"
        assert "LLM недоступна (отключено в настройках) — показаны карточки по данным." in (
            a.warnings)

    def test_parse_fallback_is_visible(self, deps: Deps) -> None:
        deps.llm = FakeLLM(parse_fail=LLMUnavailable("timeout"), error_kind="timeout")
        a = recommend(req(), deps)
        assert a.data_used.parser == "rules" and a.data_used.generation == "llm"
        assert "Запрос разобран по правилам: ИИ не ответил (таймаут)." in a.warnings
        en = recommend(req("coffee", lang="en"), deps)
        assert "The request was parsed by rules: the AI did not answer (timeout)." in (
            en.warnings)

    def test_finalize_bug_is_visible(self, deps: Deps, monkeypatch: pytest.MonkeyPatch,
                                     caplog: pytest.LogCaptureFixture) -> None:
        def boom(*_a: Any, **_k: Any) -> Any:
            raise RuntimeError("bug")

        monkeypatch.setattr(validate, "finalize", boom)
        with caplog.at_level(logging.ERROR):
            a = recommend(req(), deps)
        assert a.cards and a.data_used.generation == "template"
        assert "Тексты ИИ не прошли проверку — показаны карточки по данным." in a.warnings
        assert any("finalize failed" in r.getMessage() and "Traceback" not in r.getMessage()
                   and "line" in r.getMessage() for r in caplog.records)

    def test_route_failures_are_visible(self, deps: Deps,
                                        monkeypatch: pytest.MonkeyPatch) -> None:
        def boom(*_a: Any, **_k: Any) -> Any:
            raise RuntimeError("bug")

        monkeypatch.setattr(route, "plan_walk", boom)
        a = recommend(req("хочу прогуляться час", use_llm=False), deps)
        assert a.cards  # plan_walk crash falls back to the candidates
        assert "Маршрут построить не удалось — карточки показаны без маршрута." in a.warnings
        monkeypatch.setattr(route, "plan_route", boom)
        b = recommend(req("кафе или музей", use_llm=False), deps)
        assert b.cards and b.route is None
        assert "Маршрут построить не удалось — карточки показаны без маршрута." in b.warnings

    def test_semantic_search_down_is_visible(self, deps: Deps) -> None:
        deps.settings = dataclasses.replace(deps.settings, gemini_api_key="g-key")
        deps.store.embeddings = np.zeros((len(deps.store.pois), 4), dtype=np.float32)
        deps.llm = FakeLLM(embed=None, embed_status="daily_quota")
        a = recommend(req(), deps)
        assert ("Смысловой поиск недоступен (дневная квота Gemini) — поиск только по словам."
                in a.warnings)
        assert "поиск: по словам (дневная квота Gemini), отбор — Claude" in a.data_used.filters
        deps.llm = FakeLLM(embed=np.zeros(4, dtype=np.float32), embed_status="ok")
        bad = recommend(req(), deps)
        assert ("поиск: по словам (некорректный вектор запроса), отбор — Claude"
                in bad.data_used.filters)

    @pytest.mark.parametrize(
        ("lang", "picked", "lexical"),
        [("ru", "поиск: по словам, отбор — Claude", "поиск: по словам"),
         ("kk", "іздеу: сөздері бойынша, іріктеу — Claude", "іздеу: сөздері бойынша"),
         ("en", "search: by keywords, picked by Claude", "search: by keywords")],
    )
    def test_empty_gemini_key_is_a_choice_not_a_failure(self, deps: Deps, lang: str,
                                                        picked: str, lexical: str) -> None:
        """Video takes run with an empty GEMINI_API_KEY: no «(no key)», no warning."""
        deps.settings = dataclasses.replace(deps.settings, gemini_api_key=None)
        deps.store.embeddings = np.zeros((len(deps.store.pois), 4), dtype=np.float32)
        deps.llm = FakeLLM(embed=None, embed_status="no_key")
        a = recommend(req(lang=lang), deps)
        search = [f for f in a.data_used.filters if f.startswith(lexical)]
        assert search == [picked]
        assert not any("Gemini" in w or "Смысловой" in w or "Мағыналық" in w
                       for w in a.warnings)
        b = recommend(req(lang=lang, use_llm=False), deps)
        assert [f for f in b.data_used.filters if f.startswith(lexical)] == [lexical]

    @pytest.mark.parametrize(
        ("status", "lang", "reason"),
        [("auth", "ru", "ключ OpenRouter недействителен"),
         ("no_credit", "ru", "нет кредитов OpenRouter"),
         ("quota", "ru", "лимит запросов OpenRouter"),
         ("auth", "kk", "OpenRouter кілті жарамсыз"),
         ("no_credit", "en", "no OpenRouter credits")],
    )
    def test_openrouter_failures_are_named(self, deps: Deps, status: str, lang: str,
                                           reason: str) -> None:
        deps.settings = dataclasses.replace(deps.settings, embed_provider="openrouter",
                                            openrouter_api_key="or-key")
        deps.store.embeddings = np.zeros((len(deps.store.pois), 4), dtype=np.float32)
        deps.llm = FakeLLM(embed=None, embed_status=status)
        a = recommend(req(lang=lang), deps)
        assert any(f"({reason})" in f for f in a.data_used.filters)
        assert any(f"({reason})" in w for w in a.warnings)

    @pytest.mark.parametrize(
        ("status", "ru_reason"),
        [("minute_quota", "минутный лимит Gemini"), ("daily_quota", "дневная квота Gemini")],
    )
    def test_gemini_quota_kinds_are_named(self, deps: Deps, status: str,
                                          ru_reason: str) -> None:
        deps.settings = dataclasses.replace(deps.settings, gemini_api_key="g-key")
        deps.store.embeddings = np.zeros((len(deps.store.pois), 4), dtype=np.float32)
        deps.llm = FakeLLM(embed=None, embed_status=status)
        a = recommend(req(), deps)
        assert f"поиск: по словам ({ru_reason}), отбор — Claude" in a.data_used.filters
        assert (f"Смысловой поиск недоступен ({ru_reason}) — поиск только по словам."
                in a.warnings)
        en = recommend(req(lang="en"), deps)
        assert any("Gemini per-minute limit" in f or "Gemini daily quota" in f
                   for f in en.data_used.filters)

    def test_time_machine_weather_label(self, deps: Deps) -> None:
        a = recommend(req(use_llm=False, at="2026-01-05T15:00:00+05:00"), deps)
        assert a.data_used.weather == "погода не учтена: время задано вручную"
        now = recommend(req(use_llm=False, at=None), deps)
        assert now.data_used.weather == "погода неизвестна"

    def test_hours_note_never_mixes_languages(self, deps: Deps) -> None:
        from app.pipeline import _localize_hours

        a = recommend(req(use_llm=False), deps)
        moved = _localize_hours(a.cards, [], a.context.local_time, "en")  # no candidate
        assert all(c.open_note == "check opening hours before you go" for c in moved)
        assert _localize_hours(a.cards, [], a.context.local_time, "ru") == a.cards

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
    @pytest.mark.parametrize(("lang", "query", "landmark"), [
        ("ru", "Тихое кафе с wifi и розетками, поработать", "expo"),
        ("ru", "Куда с ребёнком в тепло, бесплатно", "khan_shatyr"),
        ("kk", "Балалармен тегін мұражай", "baiterek"),
        ("en", "Quiet cafe with wifi and power outlets, open late", "expo"),
    ])
    def test_template_whys_do_not_trip_the_tag_check(self, real_deps: Deps, lang: str,
                                                     query: str, landmark: str) -> None:
        a = recommend(real_req(query, landmark, lang=lang), real_deps)
        assert a.data_used.generation == "template" and a.cards
        for card in a.cards:
            assert not pipeline_mod._tag_leak(card.why, lang, [card.title]), card.why

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

    def test_e7_without_ai_finds_sights_near_expo(self, real_deps: Deps,
                                                  monkeypatch: pytest.MonkeyPatch) -> None:
        """The video scene «AI off — answer from the data»: E7 must still show sights."""
        names: list[str] = []
        real = retrieval.retrieve

        def spy(store: Store, intent: QueryIntent, ctx: SearchContext, qv: Any, k: int = 8
                ) -> list[Candidate]:
            out = real(store, intent, ctx, qv, k)
            names.extend(c.poi.name for c in out)
            return out

        monkeypatch.setattr(retrieval, "retrieve", spy)
        a = recommend(real_req("I'm near EXPO with one free hour. What's worth seeing nearby?",
                               "expo"), real_deps)
        assert 2 <= len(a.cards) <= 3, (a.warnings, [c.title for c in a.cards])
        assert {"attraction", "park"} <= set(a.intent.categories)
        assert "25-ти летие Независимости" in names and "Аллея мира и согласия" in names
        sightish = {"attraction", "monument", "museum", "viewpoint", "gallery", "religious",
                    "park"}
        assert all(c.category in sightish for c in a.cards)
        assert all(c.walk_min <= 20 for c in a.cards)  # «nearby» = within a 20-min walk

    def test_budget_radius_never_means_more_than_20_min_one_way(
        self, real_deps: Deps, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: list[tuple[int | None, int | None]] = []
        real = geo.radius_for

        def spy(intent: QueryIntent) -> int:
            seen.append((intent.time_budget_min, intent.max_walk_min))
            return real(intent)

        monkeypatch.setattr(geo, "radius_for", spy)
        recommend(real_req("есть 2 часа, что посмотреть?", "baiterek"), real_deps)
        assert seen[0] == (120, 20)  # 0.35 × 120 = 42 min one way would be too far
        recommend(real_req("есть полчаса, что посмотреть?", "baiterek"), real_deps)
        assert seen[-1] == (30, None)  # 0.35 × 30 ≈ 11 min: the budget decides
        recommend(real_req("2 часа, не дальше 30 минут пешком", "baiterek"), real_deps)
        assert seen[-1] == (120, 30)  # an explicit limit always wins

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
        assert a.context.area_label.startswith("сіздің ауданыңыз (≈13 га)")  # r9: 12,56 га

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


# --------------------------------------- T91: the route and card minutes on the walk graph


def _grid_graph(tmp_path: Path) -> Path:
    """Lit footways every 40 m around EXPO (x, y in metres from the walkgraph test origin)."""
    from .test_walkgraph import FOOTWAY, LIT, write_graph

    step, xs, ys = 40, range(-800, 1641, 40), range(-1300, 1101, 40)
    nodes = {f"{x}:{y}": (x, y, 0) for x in xs for y in ys}
    edges = []
    for x in xs:
        for y in ys:
            if f"{x + step}:{y}" in nodes:
                edges.append((f"{x}:{y}", f"{x + step}:{y}", FOOTWAY | LIT))
            if f"{x}:{y + step}" in nodes:
                edges.append((f"{x}:{y}", f"{x}:{y + step}", FOOTWAY | LIT))
    return write_graph(tmp_path, nodes, edges)


def _graph_deps(deps: Deps, path: Path) -> Deps:
    settings = dataclasses.replace(deps.settings, walk_graph_enabled=True, walk_graph_path=path)
    return Deps(store=deps.store, llm=deps.llm, settings=settings)


def test_route_and_card_minutes_follow_the_graph(deps: Deps, tmp_path: Path) -> None:
    import math

    from app.walkgraph import load_graph

    path = _grid_graph(tmp_path)
    ans = recommend(req("кофе", use_llm=False), _graph_deps(deps, path))
    assert len(ans.cards) >= 2 and ans.route is not None
    assert ans.route.path_source == "graph"
    assert ans.route.total_walk_m == sum(leg.distance_m for leg in ans.route.legs)
    graph = load_graph(path)
    assert graph is not None
    lengths = graph.lengths_from(ans.context.origin, {c.poi_id: c.geo for c in ans.cards},
                                 origin_landmark="expo")
    for card in ans.cards:  # distance and minutes on the card: both along the graph
        assert card.distance_m == round(lengths[card.poi_id])
        assert card.walk_min == max(1, math.ceil(lengths[card.poi_id] / 75 - 1e-9))
        assert card.why.startswith(f"≈{card.walk_min} мин")  # the reason says the same


def test_without_the_flag_or_with_a_broken_graph_the_route_is_straight(
    deps: Deps, tmp_path: Path
) -> None:
    off = recommend(req("кофе", use_llm=False), deps)  # tests run with WALK_GRAPH=false
    assert off.route is not None and off.route.path_source == "straight"
    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / "walk_graph.npz").write_bytes(b"not a zip")
    ans = recommend(req("кофе", use_llm=False), _graph_deps(deps, broken / "walk_graph.npz"))
    assert ans.route is not None and ans.route.path_source == "straight"
    assert [c.walk_min for c in ans.cards] == [c.walk_min for c in off.cards]


def test_walk_limit_holds_for_graph_minutes(
    deps: Deps, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """max_walk_min is a straight-line radius; the graph minutes on the cards keep it too."""
    from app.walkgraph import WalkGraph

    near, far = "test:poi/12", "test:poi/1"  # straight 4 and 5 min from EXPO
    lengths = {near: 280.0, far: 600.0}  # on the graph: 4 and 8 min

    def fake_lengths(self, origin, targets, mode=None, **kw):  # type: ignore[no-untyped-def]
        return {key: lengths[key] for key in targets if key in lengths}

    monkeypatch.setattr(WalkGraph, "lengths_from", fake_lengths)
    graph_deps = _graph_deps(deps, _grid_graph(tmp_path))
    ans = recommend(req("кофе", use_llm=False, overrides={"max_walk_min": 5}), graph_deps)
    assert [(c.poi_id, c.walk_min) for c in ans.cards] == [(near, 4)]  # 8 min trimmed
    assert not any("По тротуарам" in w for w in ans.warnings)

    lengths[near] = 450.0  # both over the limit on the graph: kept, and the user is told
    ans = recommend(req("кофе", use_llm=False, overrides={"max_walk_min": 5}), graph_deps)
    assert ans.cards and all(c.walk_min > 5 for c in ans.cards)
    assert any("По тротуарам до мест дольше 5 мин" in w for w in ans.warnings)


# ------------------- E2: at night, every place over the walking limit -> no walking route

NIGHT_TAXI = {"ru": "лучше на такси", "kk": "такси", "en": "taxi"}


@pytest.mark.parametrize("lang", ["ru", "kk", "en"])
def test_night_all_places_over_the_limit_no_route_and_taxi_once(deps: Deps, lang: str) -> None:
    """E2 live: 02:30, Ginza 29 min and Дастаркан 33 min over the 15-min limit, yet a 6.3 km
    walking route with 28 crossings was drawn. At night that is bad advice: cards stay,
    the route goes, the taxi is advised once (not three warnings saying the same)."""
    ans = recommend(req("куда пойти", use_llm=False, lang=lang,
                        overrides={"night": True, "max_walk_min": 4}), deps)
    assert ans.cards and all(c.walk_min > 4 for c in ans.cards)  # straight 5 and 6 min
    assert ans.route is None
    taxi = [w for w in ans.warnings if "такси" in w.lower() or "taxi" in w.lower()]
    assert len(taxi) == 1 and NIGHT_TAXI[lang] in taxi[0].lower() and "4" in taxi[0]
    assert TEXTS[lang]["night"] not in ans.warnings  # the night tip is refined, not repeated


def test_night_with_one_place_within_the_limit_keeps_the_route(deps: Deps) -> None:
    ans = recommend(req("куда пойти", use_llm=False, overrides={"night": True, "max_walk_min": 5}),
                    deps)
    assert any(c.walk_min <= 5 for c in ans.cards) and len(ans.cards) >= 2
    assert ans.route is not None
    assert TEXTS["ru"]["night"] in ans.warnings


def test_day_all_places_over_the_limit_keeps_the_route(deps: Deps) -> None:
    ans = recommend(req("куда пойти", use_llm=False, overrides={"max_walk_min": 3}), deps)
    assert len(ans.cards) >= 2 and all(c.walk_min > 3 for c in ans.cards)  # 4, 5, 6 min
    assert ans.route is not None
    assert not any("такси" in w for w in ans.warnings)


# ------------- area of an H3 cell in kk / en: hectares, exactly as the browser (cellAreaHa)


@pytest.mark.parametrize(("res", "lang", "label"), [
    (10, "kk", "сіздің ауданыңыз (≈1,8 га)"),  # r10: 1,79 га; «≈0,0 км²» before
    (10, "en", "your area (≈1.8 ha)"),
    (9, "kk", "сіздің ауданыңыз (≈13 га)"),
    (9, "en", "your area (≈13 ha)"),
])
def test_cell_area_label_in_hectares(res: int, lang: str, label: str) -> None:
    cell = h3.latlng_to_cell(51.0891, 71.4157, res)  # EXPO
    assert pipeline_mod._localized_area(lang, None, cell, "ваш район") == label  # type: ignore[arg-type]


@pytest.mark.parametrize(("ha", "kk", "en"), [
    (12.56, "13", "13"),
    (12.5, "13", "13"),  # half up, as Intl.NumberFormat (Python round() would give 12)
    (9.96, "10", "10"),  # one decimal under 10 ha, then the trailing zero goes
    (1.794, "1,8", "1.8"),
    (2.04, "2", "2"),
    (3612.3, "3\u00a0612", "3,612"),  # groups of thousands for coarse cells
])
def test_hectares_are_formatted_like_the_browser(ha: float, kk: str, en: str) -> None:
    assert pipeline_mod._format_ha(ha, "kk") == kk
    assert pipeline_mod._format_ha(ha, "en") == en


# ------------------------------------- T123: route profile -> plan_route / plan_walk / minutes


def _recording_plan_route(calls: list[dict[str, Any]]):  # type: ignore[no-untyped-def]
    def plan(origin, stops, budget_min, visit_min, max_stops=3, **kw):  # type: ignore[no-untyped-def]
        calls.append(kw)
        return fake_plan_route(origin, stops, budget_min, visit_min, max_stops,
                               loop=kw.get("loop", False), note=kw.get("note"))
    return plan


@pytest.mark.usefixtures("core_fakes")
@pytest.mark.parametrize(("profile", "overrides", "want", "line"), [
    # no graph here: the route is straight, so the panel says the profile was only asked for
    ("fastest", None, "fastest", None),
    ("lit", None, "lit", "запрошен маршрут «ночью — по крупным улицам»"),
    ("accessible", None, "accessible", "запрошен маршрут без ступенек"),
    # the wheelchair switch turns the default into a step-free way, and the panel says why
    ("fastest", {"wheelchair": True}, "accessible", "запрошен маршрут без ступенек (коляска)"),
    # an explicit choice stays (Mode.wheelchair forbids stairs on "lit" too)
    ("lit", {"wheelchair": True}, "lit", "запрошен маршрут «ночью — по крупным улицам»"),
])
def test_route_profile_is_passed_without_the_graph(
    deps: Deps, monkeypatch: pytest.MonkeyPatch, profile: str,
    overrides: dict[str, Any] | None, want: str, line: str | None,
) -> None:
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(route, "plan_route", _recording_plan_route(calls))
    a = recommend(req("кофе", use_llm=False, route_profile=profile, overrides=overrides), deps)
    assert a.route is not None and calls
    assert calls[-1]["profile"] == want and calls[-1]["lang"] == "ru"
    profile_lines = [f for f in a.data_used.filters if "маршрут" in f]
    assert profile_lines == ([line] if line else [])


@pytest.mark.parametrize(("lang", "line"), [
    ("kk", "баспалдақсыз маршрут сұралды (арба)"),
    ("en", "step-free route requested (wheelchair)")])
def test_route_profile_line_follows_the_answer_language(deps: Deps, lang: str, line: str) -> None:
    a = recommend(req("кофе", use_llm=False, lang=lang, overrides={"wheelchair": True}), deps)
    assert line in a.data_used.filters


def test_straight_route_says_the_profile_was_not_applied(deps: Deps) -> None:
    """No graph (tests run with WALK_GRAPH=false): the route is "fastest", honestly noted."""
    a = recommend(req("кофе", use_llm=False, route_profile="lit"), deps)
    assert a.route is not None and a.route.path_source == "straight"
    assert a.route.profile == "fastest"
    assert "Профиль маршрута не учтён" in a.route.note
    assert a.route.unknown_lit_share == 1.0
    en = recommend(req("coffee", use_llm=False, lang="en", route_profile="lit"), deps)
    assert en.route is not None and "Route profile not applied" in en.route.note


@pytest.mark.parametrize("profile", ["accessible", "lit"])
def test_graph_route_and_card_minutes_use_the_profile(
    deps: Deps, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, profile: str
) -> None:
    from app.walkgraph import WalkGraph

    modes: list[str] = []
    real_lengths = WalkGraph.lengths_from

    def spy(self, origin, targets, mode=None, **kw):  # type: ignore[no-untyped-def]
        modes.append(mode.profile if mode is not None else "none")
        return real_lengths(self, origin, targets, mode, **kw)

    monkeypatch.setattr(WalkGraph, "lengths_from", spy)
    a = recommend(req("кофе", use_llm=False, route_profile=profile),
                  _graph_deps(deps, _grid_graph(tmp_path)))
    assert a.route is not None and a.route.path_source == "graph"
    assert a.route.profile == profile  # the profile the route was really planned with
    assert modes and set(modes) == {profile}  # card minutes: the same Mode(profile=...)
    if profile == "lit":
        assert "маршрут: ночью — по крупным улицам" in a.data_used.filters
    else:  # «без ступенек» needs straight_share == 0 (contract 1.3.1, CORE T131)
        line = ("маршрут: без ступенек" if a.route.straight_share == 0
                else "запрошен маршрут без ступенек")
        assert line in a.data_used.filters


@pytest.mark.parametrize(("share", "steps", "line"), [
    (0.0, 0, "маршрут: без ступенек"),
    (0.2, 0, "запрошен маршрут без ступенек"),  # part straight: its steps are not known
    (None, 0, "запрошен маршрут без ступенек"),  # not counted (older CORE): not claimed
    (0.0, 2, "запрошен маршрут без ступенек"),
])
def test_step_free_is_claimed_only_at_straight_share_zero(
    deps: Deps, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, share: float | None,
    steps: int, line: str,
) -> None:
    real = route.plan_route

    def shaped(*a: Any, **kw: Any) -> MiniRoute | None:
        got = real(*a, **kw)
        return got.model_copy(update={"straight_share": share, "steps_count": steps})

    monkeypatch.setattr(route, "plan_route", shaped)
    a = recommend(req("кофе", use_llm=False, overrides={"wheelchair": True}),
                  _graph_deps(deps, _grid_graph(tmp_path)))
    assert a.route is not None and a.route.path_source == "graph"
    assert line + " (коляска)" in a.data_used.filters


def test_accessible_with_stairs_is_only_asked_for(deps: Deps, tmp_path: Path,
                                                  monkeypatch: pytest.MonkeyPatch) -> None:
    real = route.plan_route

    def with_steps(*a: Any, **kw: Any) -> MiniRoute | None:
        got = real(*a, **kw)
        return got.model_copy(update={"steps_count": 2}) if got else got

    monkeypatch.setattr(route, "plan_route", with_steps)
    a = recommend(req("кофе", use_llm=False, route_profile="accessible"),
                  _graph_deps(deps, _grid_graph(tmp_path)))
    assert a.route is not None and a.route.path_source == "graph"
    assert "запрошен маршрут без ступенек" in a.data_used.filters
    assert "маршрут: без ступенек" not in a.data_used.filters


def test_walk_loop_gets_the_profile(deps: Deps, monkeypatch: pytest.MonkeyPatch) -> None:
    got: list[str] = []
    real = route.plan_walk

    def spy(*args: Any, **kw: Any) -> list[Candidate]:
        got.append(kw.get("profile", "missing"))
        return real(*args, **kw)

    monkeypatch.setattr(route, "plan_walk", spy)
    recommend(req("прогулка на 60 минут", use_llm=False, overrides={"wheelchair": True}), deps)
    assert got == ["accessible"]


# ---------------------------------------------- T123: prefs -> search, panel, prompt, never logs

PREFS = {"likes": ["museums"], "avoids": ["bars", "cafes"]}


def test_prefs_reach_retrieval_and_the_panel(deps: Deps, monkeypatch: pytest.MonkeyPatch
                                             ) -> None:
    install_core_fakes(monkeypatch)
    seen: list[Any] = []

    def spy(store, intent, ctx, query_vec, k=8, **kw):  # type: ignore[no-untyped-def]
        seen.append(kw.get("prefs"))
        return fake_retrieve(store, intent, ctx, query_vec, k)

    monkeypatch.setattr(retrieval, "retrieve", spy)
    a = recommend(req("куда сходить", use_llm=False, prefs=PREFS), deps)
    assert seen and seen[0] is not None and seen[0].likes == ["museums"]
    assert seen[0].avoids == ["bars", "cafes"]
    assert "предпочтения: музеи; избегать: бары, кафе" in a.data_used.filters
    seen.clear()
    plain = recommend(req("куда сходить", use_llm=False, prefs={}), deps)
    assert seen == [None]  # nothing to apply: the 1.2.0 call
    assert not any("предпочтения" in f or "избегать" in f for f in plain.data_used.filters)


@pytest.mark.parametrize(("lang", "prefs", "line"), [
    ("kk", PREFS, "қалауыңыз: мұражайлар; аулақ болу: барлар, кафелер"),
    ("en", PREFS, "likes: museums; avoid: bars, cafes"),
    ("en", {"avoids": ["noisy", "long_outdoor", "kids_places"]},
     "avoid: noisy, long walks outside, kids' places"),
    ("ru", {"likes": ["warm", "kids"]}, "предпочтения: в тепле, с ребёнком"),
])
def test_prefs_line_follows_the_answer_language(deps: Deps, lang: str, prefs: dict[str, Any],
                                                line: str) -> None:
    a = recommend(req("куда сходить", use_llm=False, lang=lang, prefs=prefs), deps)
    assert line in a.data_used.filters


def test_prefs_shift_the_pick_on_the_real_core(deps: Deps) -> None:
    plain = recommend(req("куда сходить", use_llm=False), deps)
    liked = recommend(req("куда сходить", use_llm=False,
                          prefs={"likes": ["parks"], "avoids": ["bars", "cafes"]}), deps)
    before, after = [c.category for c in plain.cards], [c.category for c in liked.cards]
    assert "park" not in before and "cafe" in before
    assert after[0] == "park" and "cafe" not in after
    assert not {"bar", "nightlife"} & set(after)
    assert "по вашим предпочтениям" in liked.cards[0].why


def test_prefs_go_to_the_card_prompt_as_codes(deps: Deps, monkeypatch: pytest.MonkeyPatch
                                              ) -> None:
    prompts: list[str] = []
    llm = deps.llm
    real = llm.generate_structured  # type: ignore[union-attr]

    def spy(**kw: Any) -> Any:
        prompts.append(kw["prompt"])
        return real(**kw)

    monkeypatch.setattr(llm, "generate_structured", spy)
    recommend(req("куда сходить", prefs=PREFS), deps)
    card_prompt = next(p for p in prompts if "КАНДИДАТЫ" in p)
    assert '"likes": [\n   "museums"\n  ]' in card_prompt
    assert '"avoids": [\n   "bars",\n   "cafes"\n  ]' in card_prompt


# Every PrefTag code and its labels in ru / kk / en: none of them may show up in a log record
# (the strict form of the rule «never together with the cell, the landmark or the query»).
PREF_LOG_MARKERS = sorted(
    {*pipeline_mod.PREF_NAMES["ru"], *(v for names in pipeline_mod.PREF_NAMES.values()
                                       for v in names.values()), "prefs", "likes", "avoids"},
    key=len, reverse=True)
ALL_PREFS = {"likes": ["quiet", "museums", "parks", "warm", "night"],
             "avoids": ["bars", "noisy", "long_outdoor", "kids_places", "cafes"]}
EXCLUDE = ["test:poi/1", "test:poi/12", "event:jazz"]  # «Ещё»: ids of cards already shown


def _pref_leaks(records: list[logging.LogRecord]) -> list[str]:
    out = []
    for r in records:
        text = (r.getMessage() + " " + (r.exc_text or "")).lower()
        hits = [m for m in PREF_LOG_MARKERS if re.search(rf"(?<!\w){re.escape(m)}(?!\w)", text)]
        hits += [x for x in (*EXCLUDE, "exclude") if x in text]
        if hits:
            out.append(f"{r.name}: {hits}: {text[:120]}")
    return out


def _boom(*args: Any, **kw: Any) -> Any:
    raise RuntimeError("boom")


@pytest.mark.parametrize("location", [
    {"landmark_id": "expo"}, {"h3_cell": h3.latlng_to_cell(51.0894, 71.4161, 10)}])
@pytest.mark.parametrize("failure", [None, "retrieve", "route", "finalize", "llm", "template"])
def test_prefs_never_reach_the_logs(deps: Deps, monkeypatch: pytest.MonkeyPatch,
                                    caplog: pytest.LogCaptureFixture,
                                    location: dict[str, str], failure: str | None) -> None:
    """T123 main check (EVAL's probe is extra): prefs are labels for one answer; no log record
    carries them, so none carries them next to the cell, the landmark or the query. The same
    for exclude_poi_ids («Ещё»): no shown id in any log record."""
    if failure == "retrieve":
        monkeypatch.setattr(retrieval, "retrieve", _boom)
    elif failure == "route":
        monkeypatch.setattr(route, "plan_route", _boom)
    elif failure == "finalize":
        monkeypatch.setattr(validate, "finalize", _boom)
    elif failure == "template":
        monkeypatch.setattr(validate, "template_cards", _boom)
    elif failure == "llm":
        deps = Deps(store=deps.store, llm=FakeLLM(fail=LLMUnavailable("down")),
                    settings=deps.settings)
    query = "тихое место погулять с видом"
    body = {"query": query, "location": location, "at": DAYTIME, "prefs": ALL_PREFS,
            "exclude_poi_ids": EXCLUDE}
    request = RecommendRequest.model_validate(body)
    with caplog.at_level(logging.DEBUG):
        a = recommend(request, deps)
        logging.getLogger("app").info("request %r", request)  # even a careless repr is clean
    assert caplog.records  # the request line at least
    assert _pref_leaks(caplog.records) == []
    where = [query, location.get("landmark_id"), location.get("h3_cell")]
    for r in caplog.records:  # and the request line has no query / cell / landmark either
        if r.getMessage().startswith("recommend id="):
            assert not any(w and w in r.getMessage() for w in where)
    assert a.data_used.filters  # while the answer itself may say them


# ------------------------------------------- T123: «в тепле» — indoor stops, short walks outside

WARM_LINE = "в тепле: на улице до 10 мин подряд"


def _frost(temp_c: float):  # type: ignore[no-untyped-def]
    def get(origin: GeoPoint, at: datetime) -> WeatherInfo:
        return WeatherInfo(temp_c=temp_c, summary=f"{temp_c:+.0f}°C", outdoor_ok=temp_c > -10,
                           fetched_at=datetime(2026, 10, 19, 10, 0, tzinfo=UTC))
    return get


def test_warm_like_gives_indoor_stops_close_together(deps: Deps) -> None:
    by_id = deps.store.by_id
    plain = recommend(req("погулять", use_llm=False), deps)
    assert any(not retrieval.is_indoor(by_id[c.poi_id]) for c in plain.cards)  # park, view
    a = recommend(req("погулять", use_llm=False, prefs={"likes": ["warm"]}), deps)
    # indoor by the record (the test cafe has no "indoor" tag; OSM data always has it)
    assert len(a.cards) >= 2 and all(retrieval.is_indoor(by_id[c.poi_id]) for c in a.cards)
    assert a.route is not None and a.route.max_outdoor_leg_min is not None
    assert a.route.max_outdoor_leg_min <= 10
    assert all(c.walk_min <= 10 for c in a.cards)
    assert WARM_LINE in a.data_used.filters and "в помещении" in a.data_used.filters
    assert t_no_match() not in a.warnings  # «погулять» is not unmatched in the warm


def t_no_match() -> str:
    return TEXTS["ru"]["no_match"]


def test_warm_by_the_indoor_switch_and_by_frost(deps: Deps, monkeypatch: pytest.MonkeyPatch
                                                ) -> None:
    switch = recommend(req("куда сходить", use_llm=False, overrides={"indoor_only": True}), deps)
    assert WARM_LINE in switch.data_used.filters
    assert not any(f.startswith("предпочтения") for f in switch.data_used.filters)
    monkeypatch.setattr(weather, "get_weather", _frost(-20))
    frost = recommend(req("куда сходить", use_llm=False), deps)
    assert f"{WARM_LINE} (мороз −20 °C)" in frost.data_used.filters
    assert all(retrieval.is_indoor(deps.store.by_id[c.poi_id]) for c in frost.cards)
    en = recommend(req("where to go", use_llm=False, lang="en"), deps)
    assert "warm: up to 10 min outside at a time (frost −20 °C)" in en.data_used.filters
    kk = recommend(req("қайда баруға болады", use_llm=False, lang="kk"), deps)
    assert "жылыда: далада қатарынан 10 минутқа дейін (аяз −20 °C)" in kk.data_used.filters
    monkeypatch.setattr(weather, "get_weather", _frost(-9))
    mild = recommend(req("куда сходить", use_llm=False), deps)
    assert not any(f.startswith("в тепле:") for f in mild.data_used.filters)


def test_warm_has_no_walk_loop(deps: Deps, monkeypatch: pytest.MonkeyPatch) -> None:
    """A place at the start (inside the mall you stand in) is a fine warm stop; plan_walk would
    skip it (AT_ORIGIN_M) and send the person out to a far one."""
    calls: list[int] = []
    real = route.plan_walk
    monkeypatch.setattr(route, "plan_walk", lambda *a, **kw: calls.append(1) or real(*a, **kw))
    a = recommend(req("прогулка на 60 минут", use_llm=False, prefs={"likes": ["warm"]}), deps)
    assert calls == [] and a.cards
    assert a.route is None or a.route.legs[-1].to_id != "origin"


def test_warm_cluster_keeps_stops_within_a_short_walk(synthetic_pois: list[POI]) -> None:
    by_id = {p.id: p for p in synthetic_pois}

    def cand(pid: str) -> Candidate:
        return Candidate(poi=by_id[pid], distance_m=100, walk_min=2, score=0.5,
                         hours=HoursVerdict(status="unknown", confidence="low", note="-"))

    # 7 mall, 1 cafe, 9 restaurant: close together; 2 cafe 600 m east; 4 park outdoor
    picked = pipeline_mod.warm_cluster([cand(i) for i in (
        "test:poi/7", "test:poi/4", "test:poi/2", "test:poi/1", "test:poi/9")])
    ids = [c.poi.id for c in picked]
    assert ids[0] == "test:poi/7" and "test:poi/4" not in ids  # outdoor out: enough indoor
    for a in picked:
        for b in picked:
            assert geo.walk_minutes(geo.haversine_m(a.poi.geo, b.poi.geo)) <= 10
    # one indoor place only: outdoor ones stay in the pool, the far mall still goes
    alone = pipeline_mod.warm_cluster([cand("test:poi/4"), cand("test:poi/7")])
    assert [c.poi.id for c in alone] == ["test:poi/4"]


def test_warm_with_too_few_indoor_places_says_so(deps: Deps) -> None:
    only_outdoor = [p for p in deps.store.pois if p.category in ("park", "viewpoint", "monument",
                                                                   "playground")]
    store = make_store(only_outdoor, deps.store.landmarks)
    a = recommend(req("куда сходить", use_llm=False, prefs={"likes": ["warm"]}),
                  Deps(store=store, llm=deps.llm, settings=deps.settings))
    assert a.cards
    assert TEXTS["ru"]["warm_outdoor"] in a.warnings


def test_warm_long_walk_outside_is_told(deps: Deps, monkeypatch: pytest.MonkeyPatch) -> None:
    real = route.plan_route

    def long_leg(*a: Any, **kw: Any) -> MiniRoute | None:
        got = real(*a, **kw)
        return got.model_copy(update={"max_outdoor_leg_min": 14}) if got else got

    monkeypatch.setattr(route, "plan_route", long_leg)
    a = recommend(req("куда сходить", use_llm=False, prefs={"likes": ["warm"]}), deps)
    assert a.route is not None
    assert TEXTS["ru"]["warm_long_leg"].format(n=14) in a.warnings


# ------------------------------------------------- T123: events -> kind="event" cards (no net)


def _event_record(slug: str = "jazz", starts: str = "2026-10-19T19:00:00+05:00",
                  ends: str | None = "2026-10-19T21:00:00+05:00", **kw: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "id": f"event:{slug}", "title": {"ru": "Джазовый вечер", "kk": "Джаз кеші",
                                         "en": "Jazz night"},
        "category": "theatre", "tags": ["indoor", "live_music"],
        "starts_at": starts, "ends_at": ends,
        "venue": "Тест-музей", "address": "ул. Тестовая, 3",
        "geo": {"lat": 51.0882, "lon": 71.4158},  # ~140 m from EXPO, next to test:poi/3
        "venue_poi_id": "test:poi/3", "source_url": "https://example-venue.kz/afisha/jazz",
        "source_name": "Тест-площадка", "fetched_at": "2026-10-18", "indoor": True,
    }
    data.update(kw)
    return data


def _events_deps(deps: Deps, tmp_path: Path, items: object) -> Deps:
    data_dir = tmp_path / "data"
    data_dir.mkdir(exist_ok=True)
    (data_dir / "events.json").write_text(json.dumps(items, ensure_ascii=False),
                                          encoding="utf-8")
    return Deps(store=deps.store, llm=deps.llm,
                settings=dataclasses.replace(deps.settings, data_dir=data_dir,
                                             events_path=data_dir / "events.json"))


EVENT_Q = "концерт или спектакль сегодня вечером"


def test_event_becomes_an_event_card_on_the_template_path(deps: Deps, tmp_path: Path) -> None:
    d = _events_deps(deps, tmp_path, [_event_record()])
    a = recommend(req(EVENT_Q, use_llm=False), d)
    ev_cards = [c for c in a.cards if c.kind == "event"]
    assert len(ev_cards) == 1
    c = ev_cards[0]
    assert c.poi_id == "event:jazz" and c.title == "Джазовый вечер"
    assert c.venue == "Тест-музей" and c.address == "ул. Тестовая, 3"  # 1.3.1
    assert c.facts_sources == ["manual"] and c.idea_source == "poi_data"
    assert c.starts_at == datetime(2026, 10, 19, 19, 0, tzinfo=ASTANA_TZ)
    assert c.ends_at == datetime(2026, 10, 19, 21, 0, tzinfo=ASTANA_TZ)
    assert c.source_url == "https://example-venue.kz/afisha/jazz"
    assert (c.open_status, c.open_confidence) == ("unknown", "low")
    assert c.open_note == "Начало в 19:00; окончание в 21:00" + CHECK_RU
    assert "начало в 19:00" in c.why and "часы работы не указаны" not in c.why
    assert (c.visit_min_low, c.visit_min_high) == (120, 120)
    assert "manual" in a.data_used.sources
    # the event is not an «unknown hours» place in the warnings
    unknown = sum(x.open_status == "unknown" and x.kind == "place" for x in a.cards)
    hours_lines = [w for w in a.warnings if "нет данных о часах" in w]
    assert hours_lines == ([] if not unknown else [
        TEXTS["ru"]["hours_unknown_1"] if unknown == 1
        else TEXTS["ru"]["hours_unknown_n"].format(n=unknown)])
    Card.model_validate(c.model_dump())  # the contract's event rules hold


def test_the_venue_gives_its_place_to_its_event(deps: Deps, tmp_path: Path) -> None:
    tuesday = "2026-10-20T15:00:00+05:00"  # the test museum (the venue) is open on Tuesdays
    plain = recommend(req("музей", use_llm=False, at=tuesday), deps)
    assert "test:poi/3" in [c.poi_id for c in plain.cards]
    show = _event_record("show", category="museum", title={"ru": "Выставка в музее"},
                         starts="2026-10-20T11:00:00+05:00", ends="2026-10-20T19:00:00+05:00")
    a = recommend(req("музей", use_llm=False, at=tuesday), _events_deps(deps, tmp_path, [show]))
    ids = [c.poi_id for c in a.cards]
    assert "event:show" in ids and "test:poi/3" not in ids


def test_event_card_through_the_llm_path(deps: Deps, tmp_path: Path) -> None:
    """The model writes «идёт сейчас» about an event that starts at 19:00: the sentence goes."""
    llm = FakeLLM(whys=["Идёт сейчас! Джаз рядом с вами.", "Рядом и подходит."])
    d = _events_deps(Deps(store=deps.store, llm=llm, settings=deps.settings), tmp_path,
                     [_event_record()])
    prompts: list[str] = []
    real = llm.generate_structured

    def spy(**kw: Any) -> Any:
        prompts.append(kw["prompt"])
        return real(**kw)

    llm.generate_structured = spy  # type: ignore[method-assign]
    a = recommend(req(EVENT_Q), d)
    card_prompt = next(p for p in prompts if "КАНДИДАТЫ" in p)
    assert '"kind": "event"' in card_prompt and '"on_now": false' in card_prompt
    assert '"starts_local": "2026-10-19 19:00"' in card_prompt
    ev_cards = [c for c in a.cards if c.kind == "event"]
    assert ev_cards and a.data_used.generation == "llm"
    assert "Идёт сейчас" not in ev_cards[0].why and "Джаз рядом" in ev_cards[0].why
    assert ev_cards[0].source_url and ev_cards[0].facts_sources == ["manual"]


def test_on_now_only_while_the_event_is_on(deps: Deps, tmp_path: Path) -> None:
    d = _events_deps(deps, tmp_path, [_event_record()])
    a = recommend(req(EVENT_Q, use_llm=False, at="2026-10-19T19:30:00+05:00"), d)
    c = next(c for c in a.cards if c.kind == "event")
    assert c.open_note == "Идёт сейчас: 19:00–21:00 — по данным источника, проверьте перед визитом."
    assert "идёт сейчас, до 21:00" in c.why
    assert (c.visit_min_low, c.visit_min_high) == (90, 90)  # what is left of it
    no_end = _events_deps(deps, tmp_path, [_event_record(ends=None)])
    b = recommend(req(EVENT_Q, use_llm=False, at="2026-10-19T19:30:00+05:00"), no_end)
    c = next(c for c in b.cards if c.kind == "event")
    assert c.ends_at is None and c.open_note == "Начало в 19:00" + CHECK_RU
    assert "идёт" not in c.why.lower()


@pytest.mark.parametrize(("lang", "title", "note"), [
    ("kk", "Джаз кеші", "Басталуы: 19:00; аяқталуы: 21:00" + CHECK_KK),
    ("en", "Jazz night", "Starts at 19:00; ends at 21:00 — per the source, check before you go."),
])
def test_event_card_in_kk_and_en(deps: Deps, tmp_path: Path, lang: str, title: str,
                                 note: str) -> None:
    d = _events_deps(deps, tmp_path, [_event_record()])
    a = recommend(req("concert tonight" if lang == "en" else "бүгін кешке концерт",
                      use_llm=False, lang=lang), d)
    c = next(c for c in a.cards if c.kind == "event")
    assert c.title == title and c.open_note == note


def test_past_far_and_missing_events_give_no_event_card(deps: Deps, tmp_path: Path) -> None:
    past = _event_record("past", starts="2026-10-19T10:00:00+05:00",
                         ends="2026-10-19T12:00:00+05:00")
    far = _event_record("far", geo={"lat": 51.16, "lon": 71.47})  # ~9 km away
    later = _event_record("later", starts="2026-11-01T19:00:00+05:00", ends=None)
    for items in ([past, far, later], [], {"broken": True}):
        a = recommend(req(EVENT_Q, use_llm=False), _events_deps(deps, tmp_path, items))
        assert a.cards and not any(c.kind == "event" for c in a.cards)
    missing = Deps(store=deps.store, llm=deps.llm,
                   settings=dataclasses.replace(deps.settings,
                                                events_path=tmp_path / "none" / "events.json"))
    a = recommend(req(EVENT_Q, use_llm=False), missing)
    assert a.cards and not any(c.kind == "event" for c in a.cards)


def test_events_follow_the_hard_filters(deps: Deps, tmp_path: Path) -> None:
    d = _events_deps(deps, tmp_path, [_event_record()])
    cafe = recommend(req("кофе", use_llm=False, overrides={"categories": ["cafe"]}), d)
    assert not any(c.kind == "event" for c in cafe.cards)  # categories still filter
    kids = recommend(req(EVENT_Q, use_llm=False, overrides={"with_kids": True}), d)
    assert not any(c.kind == "event" for c in kids.cards)  # not marked kids_ok


def test_a_live_event_passes_the_night_filter(deps: Deps, tmp_path: Path) -> None:
    late = _event_record("late", starts="2026-10-19T22:00:00+05:00",
                         ends="2026-10-19T23:59:00+05:00")
    d = _events_deps(deps, tmp_path, [late])
    a = recommend(req(EVENT_Q, use_llm=False, at="2026-10-19T23:00:00+05:00"), d)
    c = next(c for c in a.cards if c.kind == "event")
    assert c.open_note.startswith("Идёт сейчас: 22:00–23:59")


def test_event_card_prompt_rule_exists() -> None:
    from app.llm import load_prompt

    system = load_prompt("generate_cards")
    assert 'kind = "event"' in system and "on_now" in system and "event_note" in system


def test_a_broken_event_never_takes_the_places_down(deps: Deps, tmp_path: Path,
                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    real = retrieval.retrieve

    def fragile(*a: Any, **kw: Any) -> list[Candidate]:
        if kw.get("events"):
            raise ValueError("bad event record")
        return real(*a, **kw)

    monkeypatch.setattr(retrieval, "retrieve", fragile)
    a = recommend(req(EVENT_Q, use_llm=False), _events_deps(deps, tmp_path, [_event_record()]))
    assert a.cards and not any(c.kind == "event" for c in a.cards)
    assert TEXTS["ru"]["search_error"] not in a.warnings


# ------------------------------------- «Ещё» (contract 1.3.0, exclude_poi_ids): no repeats

MORE_Q = "куда сходить сегодня"


@pytest.mark.parametrize("use_llm", [False, True])
def test_more_never_repeats_the_cards_already_shown(deps: Deps, tmp_path: Path,
                                                    use_llm: bool) -> None:
    d = _events_deps(deps, tmp_path, [_event_record()])
    first = recommend(req(MORE_Q, use_llm=use_llm), d)
    shown = [c.poi_id for c in first.cards]
    assert len(shown) >= 2 and "event:jazz" in shown  # an event among them too
    second = recommend(req(MORE_Q, use_llm=use_llm, exclude_poi_ids=shown), d)
    assert second.cards and not set(shown) & {c.poi_id for c in second.cards}
    assert second.route is None or not set(shown) & set(second.route.stop_ids)
    assert "без уже показанных мест: " + str(len(shown)) in second.data_used.filters
    third = recommend(req(MORE_Q, use_llm=use_llm,
                          exclude_poi_ids=shown + [c.poi_id for c in second.cards]), d)
    assert not set(shown + [c.poi_id for c in second.cards]) & {c.poi_id for c in third.cards}


def test_more_with_nothing_left_is_an_honest_empty_answer(deps: Deps) -> None:
    near = [p for p in deps.store.pois if p.id in ("test:poi/1", "test:poi/12")]
    small = Deps(store=make_store(near, deps.store.landmarks), llm=deps.llm,
                 settings=deps.settings)
    first = recommend(req("кофе", use_llm=False), small)
    assert {c.poi_id for c in first.cards} == {"test:poi/1", "test:poi/12"}
    a = recommend(req("кофе", use_llm=False, exclude_poi_ids=["test:poi/1", "test:poi/12"]),
                  small)
    assert a.cards == [] and a.route is None
    assert a.summary == TEXTS["ru"]["summary_no_more"]
    assert TEXTS["ru"]["nothing"] in a.warnings
    en = recommend(req("coffee", use_llm=False, lang="en",
                       exclude_poi_ids=["test:poi/1", "test:poi/12"]), small)
    assert en.summary == TEXTS["en"]["summary_no_more"]


def test_more_keeps_the_ids_out_of_the_retrieve_reasons(deps: Deps,
                                                        monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[Any] = []
    real = retrieval.retrieve

    def spy(*a: Any, **kw: Any) -> list[Candidate]:
        seen.append(kw.get("exclude_ids"))
        return real(*a, **kw)

    monkeypatch.setattr(retrieval, "retrieve", spy)
    recommend(req("кофе", use_llm=False), deps)
    assert seen == [None]  # nothing to exclude: the 1.2.0 call
    seen.clear()
    recommend(req("кофе", use_llm=False, exclude_poi_ids=["test:poi/1"]), deps)
    assert seen and seen[0] == frozenset({"test:poi/1"})



# ---------------------------------------------- review de49d92: events, frost limit, «Ещё»


def test_llm_summary_cannot_say_on_now_about_an_event_that_is_not_on(deps: Deps,
                                                                    tmp_path: Path) -> None:
    """Review BLOCK 1: «Джазовый вечер уже идёт совсем рядом.» while the card says 19:00."""
    say = "Джазовый вечер уже идёт совсем рядом."
    llm = FakeLLM(summary=say)
    d = _events_deps(Deps(store=deps.store, llm=llm, settings=deps.settings), tmp_path,
                     [_event_record()])
    a = recommend(req(MORE_Q), d)
    assert a.data_used.generation == "llm" and any(c.kind == "event" for c in a.cards)
    assert "идёт" not in a.summary.lower()
    on = recommend(req(MORE_Q, at="2026-10-19T19:30:00+05:00"), d)  # 19:00–21:00: on now
    ev_cards = [c for c in on.cards if c.kind == "event"]
    assert ev_cards and all(c.open_note.startswith("Идёт сейчас") for c in ev_cards)
    assert on.summary == say  # true while it is on


def test_llm_event_card_takes_the_visit_minutes_from_the_event(deps: Deps,
                                                               tmp_path: Path) -> None:
    d = _events_deps(deps, tmp_path, [_event_record()])  # FakeLLM says 20–30 min
    a = recommend(req(MORE_Q), d)
    c = next(c for c in a.cards if c.kind == "event")
    assert a.data_used.generation == "llm"
    assert (c.visit_min_low, c.visit_min_high) == (120, 120)  # the 2-hour session


RUN_RECORD = {"starts": "2026-10-01T10:00:00+05:00", "ends": "2026-11-30T18:00:00+05:00"}


def test_a_run_of_many_days_is_not_on_now_and_not_night_sure(deps: Deps, tmp_path: Path
                                                             ) -> None:
    """Review BLOCK 2: an exhibition 01.10–30.11 at 23:00 is not «идёт сейчас: 10:00–18:00»
    and does not pass the night filter as «surely on»."""
    run = _event_record("expo-run", category="museum", title={"ru": "Выставка"}, **RUN_RECORD)
    d = _events_deps(deps, tmp_path, [run])
    day = recommend(req("выставка", use_llm=False), d)
    c = next(c for c in day.cards if c.kind == "event")
    assert c.open_note.startswith("Проходит до 30.11 18:00, часы работы площадки не указаны")
    assert "проходит до 30.11 18:00" in c.why and "идёт сейчас" not in c.why
    assert (c.visit_min_low, c.visit_min_high) == (60, 120)  # the museum estimate, not 300
    night = recommend(req("куда сходить", use_llm=False, at="2026-10-19T23:00:00+05:00"), d)
    assert not any(c.kind == "event" for c in night.cards)


def test_an_explicit_walk_limit_survives_warm_mode(deps: Deps,
                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(weather, "get_weather", _frost(-20))
    a = recommend(req("куда сходить", use_llm=False, overrides={"max_walk_min": 30}), deps)
    assert "пешком до 30 мин" in a.data_used.filters  # not cut to 10 silently
    assert "в тепле: между местами до 10 мин пешком (мороз −20 °C)" in a.data_used.filters
    capped = recommend(req("куда сходить", use_llm=False), deps)
    assert "пешком до 10 мин" in capped.data_used.filters


def test_no_more_summary_only_for_an_honestly_empty_pick(deps: Deps,
                                                         monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(retrieval, "retrieve", _boom)
    a = recommend(req("кофе", use_llm=False, exclude_poi_ids=["test:poi/1"]), deps)
    assert a.cards == []
    assert a.summary != TEXTS["ru"]["summary_no_more"]  # a failed search is not «no more»
    assert TEXTS["ru"]["search_error"] in a.warnings


def test_a_cut_one_sentence_llm_summary_falls_back_to_the_template(deps: Deps,
                                                                   tmp_path: Path) -> None:
    """Review nit: the model summary was one «идёт сейчас» sentence, cut -> not an empty
    summary but the template one."""
    llm = FakeLLM(summary="Джазовый вечер уже идёт совсем рядом.")
    d = _events_deps(Deps(store=deps.store, llm=llm, settings=deps.settings), tmp_path,
                     [_event_record()])
    a = recommend(req(MORE_Q), d)
    assert a.data_used.generation == "llm"
    assert a.summary == TEXTS["ru"]["summary_n"].format(n=len(a.cards))
    llm.summary = "Тут открыто сейчас."  # an «open now» claim without open/high
    b = recommend(req("кофе"), deps_with(llm, deps))
    assert b.summary.strip() and "открыто сейчас" not in b.summary


def deps_with(llm: FakeLLM, deps: Deps) -> Deps:
    return Deps(store=deps.store, llm=llm, settings=deps.settings)


def test_llm_run_card_gets_the_category_visit_estimate(deps: Deps, tmp_path: Path) -> None:
    run = _event_record("expo-run", category="museum", title={"ru": "Выставка"}, **RUN_RECORD)
    a = recommend(req("выставка"), _events_deps(deps, tmp_path, [run]))  # FakeLLM: 20–30 min
    c = next(c for c in a.cards if c.kind == "event")
    assert a.data_used.generation == "llm"
    assert (c.visit_min_low, c.visit_min_high) == (60, 120)  # the museum estimate


# --------------------- an explicit «избегать» beats what the parser read in the text (UX check)


def test_avoids_take_the_parsed_category_out_of_the_search(deps: Deps) -> None:
    """«Хочу тихое место с кофе» + the «Без кафе» chip (avoids cafes): three cafes and the
    panel saying «категории: кафе» next to «избегать: кофе» before. Now the parsed cafe
    category and the coffee tag leave the search; the panel does not contradict itself."""
    q = "Хочу тихое место с кофе"
    plain = recommend(req(q, use_llm=False), deps)
    assert plain.cards and all(c.category == "cafe" for c in plain.cards)
    a = recommend(req(q, use_llm=False, prefs={"avoids": ["cafes"]}), deps)
    # an avoid is a penalty, not a filter: the one quiet place of the test data (a cafe) may
    # stay, the other cafes go
    assert a.cards and sum(c.category == "cafe" for c in a.cards) <= 1
    f = a.data_used.filters
    assert not any(x.startswith("категории:") and "кафе" in x for x in f)
    assert not any(x.startswith("теги:") and "кофе" in x for x in f)
    assert "избегать: кафе" in f
    assert a.intent.categories == ["cafe"]  # what was asked stays visible in the intent


def test_explicit_ui_categories_stay_with_avoids(deps: Deps) -> None:
    a = recommend(req("Хочу тихое место с кофе", use_llm=False, prefs={"avoids": ["cafes"]},
                      overrides={"categories": ["cafe"]}), deps)
    assert a.cards and all(c.category == "cafe" for c in a.cards)  # the UI asked for cafes
    assert "категории: кафе" in a.data_used.filters


@pytest.mark.parametrize(("query", "label", "category"), [
    ("кафе рядом", "cafes", "cafe"), ("бар рядом", "bars", "bar")])
def test_avoids_drop_their_categories(deps: Deps, query: str, label: str,
                                      category: str) -> None:
    late = "2026-10-19T20:00:00+05:00"  # the test bar is open (18:00–04:00)
    name = pipeline_mod.CATEGORY_NAMES["ru"][category]
    plain = recommend(req(query, use_llm=False, at=late), deps)
    assert any(name in x for x in plain.data_used.filters if x.startswith("категории:"))
    a = recommend(req(query, use_llm=False, at=late, prefs={"avoids": [label]}), deps)
    assert not any(name in x for x in a.data_used.filters if x.startswith("категории:"))
    # the query word («кафе», «бар») no longer pulls the avoided place up either
    assert a.cards and not any(c.category == category for c in a.cards)


def test_the_no_cafe_chip_on_a_coffee_query(deps: Deps) -> None:
    """«кофе рядом» + «Без кафе»: the word «кофе» does not pull cafes up against the chip."""
    near = [p for p in deps.store.pois if p.id in ("test:poi/12", "test:poi/7", "test:poi/9")]
    small = Deps(store=make_store(near, deps.store.landmarks), llm=deps.llm,
                 settings=deps.settings)
    plain = recommend(req("кофе рядом", use_llm=False), small)
    assert [c.category for c in plain.cards] == ["cafe"]  # the category filter of the text
    a = recommend(req("кофе рядом", use_llm=False, prefs={"avoids": ["cafes"]}), small)
    assert a.cards and a.cards[0].category != "cafe"
    assert TEXTS["ru"]["no_match"] not in a.warnings  # the avoided word is not «unmatched»


def test_avoided_categories_and_tags_follow_the_table() -> None:
    from app.schemas import UserPrefs

    cats, tags = retrieval.avoided(UserPrefs(avoids=["cafes", "bars", "noisy"]))
    assert cats == {"cafe", "bar", "nightlife"}  # noisy is a penalty only, not the parse
    assert tags == {"coffee"}
    assert retrieval.avoided(None) == (frozenset(), frozenset())


# ----------------------------------- T133: avoids long_outdoor — short walks, indoor not needed


def test_long_outdoor_keeps_walks_outside_short_without_the_indoor_filter(
    deps: Deps, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[int] = []
    real = route.plan_walk
    monkeypatch.setattr(route, "plan_walk", lambda *a, **kw: calls.append(1) or real(*a, **kw))
    a = recommend(req("погулять", use_llm=False, prefs={"avoids": ["long_outdoor"]}), deps)
    f = a.data_used.filters
    assert "без долгих отрезков на улице: до 10 мин подряд" in f
    assert "в помещении" not in f and not any(x.startswith("в тепле") for x in f)
    assert "пешком до 10 мин" in f and a.cards
    for x in a.cards:
        for y in a.cards:
            assert geo.walk_minutes(geo.haversine_m(x.geo, y.geo)) <= 10
    assert calls == []  # no walk loop: short legs, one way
    en = recommend(req("walk", use_llm=False, lang="en", prefs={"avoids": ["long_outdoor"]}),
                   deps)
    assert "no long walks outside: up to 10 min at a time" in en.data_used.filters


# ------------------------------- T133: an event in the route — the last stop, or not a stop


def _last_stop_fake(calls: list[dict[str, Any]]):  # type: ignore[no-untyped-def]
    """plan_route of CORE T131 (last_stop_id=...), faked: records the call, puts it last."""
    real = route.plan_route

    def plan(origin, stops, budget_min, visit_min, max_stops=3, *, last_stop_id=None,
             **kw):  # type: ignore[no-untyped-def]
        calls.append({"stops": [s.poi.id for s in stops], "last": last_stop_id})
        got = real(origin, stops, budget_min, visit_min, max_stops, **kw)
        if got is None or last_stop_id is None:
            return got
        ids = [i for i in got.stop_ids if i != last_stop_id] + [last_stop_id]
        return got.model_copy(update={"stop_ids": ids})
    return plan


@pytest.mark.parametrize(("at", "is_stop"), [
    ("2026-10-19T18:45:00+05:00", True),  # 15 min before the start
    ("2026-10-19T18:30:00+05:00", True),  # exactly 30 min
    ("2026-10-19T19:30:00+05:00", True),  # on now
    ("2026-10-19T18:00:00+05:00", False),  # 60 min before: a card, not a stop
])
def test_event_is_the_last_stop_or_not_a_stop(deps: Deps, tmp_path: Path,
                                              monkeypatch: pytest.MonkeyPatch, at: str,
                                              is_stop: bool) -> None:
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(route, "plan_route", _last_stop_fake(calls))
    d = _events_deps(deps, tmp_path, [_event_record()])
    a = recommend(req(MORE_Q, use_llm=False, at=at), d)
    assert any(c.kind == "event" for c in a.cards)  # the card stays either way
    assert a.route is not None and calls
    if is_stop:
        assert calls[-1]["last"] == "event:jazz" and "event:jazz" in calls[-1]["stops"]
        assert a.route.stop_ids[-1] == "event:jazz"
    else:
        assert calls[-1]["last"] is None and "event:jazz" not in calls[-1]["stops"]
        assert "event:jazz" not in a.route.stop_ids


def test_of_two_soon_events_the_earlier_ends_the_route(deps: Deps, tmp_path: Path,
                                                       monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(route, "plan_route", _last_stop_fake(calls))
    early = _event_record("early", starts="2026-10-19T18:50:00+05:00",
                          ends="2026-10-19T20:00:00+05:00")
    late = _event_record("late", starts="2026-10-19T19:10:00+05:00",
                         ends="2026-10-19T21:00:00+05:00",
                         geo={"lat": 51.0886, "lon": 71.4170}, venue_poi_id=None)
    a = recommend(req(MORE_Q, use_llm=False, at="2026-10-19T18:45:00+05:00"),
                  _events_deps(deps, tmp_path, [early, late]))
    shown = {c.poi_id for c in a.cards if c.kind == "event"}
    assert shown == {"event:early", "event:late"}
    assert calls[-1]["last"] == "event:early" and "event:late" not in calls[-1]["stops"]


def test_without_last_stop_support_an_event_is_never_a_stop(deps: Deps, tmp_path: Path
                                                            ) -> None:
    """Before CORE T131 is merged plan_route has no last_stop_id: the event stays a card and
    the route covers the places only, so «the event stop is the last one» always holds."""
    import inspect

    if "last_stop_id" in inspect.signature(route.plan_route).parameters:
        pytest.skip("CORE plan_route already takes last_stop_id")
    d = _events_deps(deps, tmp_path, [_event_record()])
    a = recommend(req(MORE_Q, use_llm=False, at="2026-10-19T18:45:00+05:00"), d)
    assert any(c.kind == "event" for c in a.cards)
    assert a.route is None or "event:jazz" not in a.route.stop_ids



# ------------------------------- T133: the weather service did not answer in time — say so

REAL_GET_WEATHER = weather.get_weather  # taken before conftest swaps it for «no weather»


@pytest.mark.parametrize(("lang", "line"), [
    ("ru", "погода: нет данных (сервис не ответил)"),
    ("kk", "ауа райы: дерек жоқ (қызмет жауап бермеді)"),
    ("en", "weather: no data (the service did not answer)"),
])
def test_no_answer_from_the_weather_service_is_said(deps: Deps, monkeypatch: pytest.MonkeyPatch,
                                                    lang: str, line: str) -> None:
    import httpx

    def slow(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    weather._cache.clear()
    monkeypatch.setattr(weather, "get_weather", REAL_GET_WEATHER)
    monkeypatch.setattr(weather, "_make_client", lambda timeout_s=1.5: httpx.Client(
        transport=httpx.MockTransport(slow)))
    body = {"query": "кофе", "location": {"landmark_id": "expo"}, "use_llm": False,
            "lang": lang}  # no `at`: now, so the weather is asked
    a = recommend(RecommendRequest.model_validate(body), deps)
    assert a.data_used.weather == line  # (no card check: `now` may be night)
    assert "open_meteo" not in a.data_used.sources
    weather._cache.clear()


def test_without_a_status_the_old_label_stays(deps: Deps) -> None:
    """conftest's «no weather» stand-in sets no status: «погода неизвестна», as before."""
    body = {"query": "кофе", "location": {"landmark_id": "expo"}, "use_llm": False}
    a = recommend(RecommendRequest.model_validate(body), deps)
    assert a.data_used.weather == TEXTS["ru"]["weather_unknown"]


# ------------------------- CORE T131 merged: the real last_stop_id, and a short time budget


def test_with_core_last_stop_id_the_event_really_ends_the_route(deps: Deps, tmp_path: Path
                                                                ) -> None:
    d = _events_deps(deps, tmp_path, [_event_record()])
    a = recommend(req(MORE_Q, use_llm=False, at="2026-10-19T18:45:00+05:00"), d)
    assert a.route is not None and a.route.stop_ids[-1] == "event:jazz"
    later = recommend(req(MORE_Q, use_llm=False, at="2026-10-19T18:00:00+05:00"), d)
    assert any(c.kind == "event" for c in later.cards)
    assert later.route is None or "event:jazz" not in later.route.stop_ids


def test_a_short_budget_drops_a_place_not_the_event(deps: Deps) -> None:
    """CORE drops the lowest-ranked stop when the time is short. The event is card 3 here, yet
    it goes first in the stops (kept longest): a place can wait, the event will not."""
    from app import events as ev

    now = datetime(2026, 10, 19, 18, 45, tzinfo=ASTANA_TZ)
    show = ev.Event.model_validate(_event_record(ends="2026-10-19T19:20:00+05:00"))
    by_id = deps.store.by_id
    ev_cand = Candidate(poi=ev.as_poi(show, "ru"), distance_m=150, walk_min=3, score=0.3,
                        hours=ev.verdict(show, now, "ru"))
    places = [Candidate(poi=by_id[i], distance_m=300, walk_min=5, score=0.6,
                        hours=HoursVerdict(status="unknown", confidence="low", note="-"))
              for i in ("test:poi/7", "test:poi/1")]
    intent = QueryIntent(raw_query="x", time_budget_min=40)
    ctx = SearchContext(origin=GeoPoint(lat=51.0894, lon=71.4161), h3_cell="8a2153806d1ffff",
                        h3_res=10, area_label="у EXPO", local_time=now, radius_m=1200)
    cards = validate.template_cards([*places, ev_cand], intent, ctx, events={show.id: show})
    assert [c.poi_id for c in cards][-1] == show.id  # the event is the lowest-ranked card
    got, failed = pipeline_mod._route(ctx, cards, [*places, ev_cand], intent, True, "rid")
    assert not failed and got is not None
    assert got.stop_ids[-1] == show.id  # kept, and last
    assert len(got.stop_ids) < 3  # a place was dropped for the time instead
    two_hours = ev.Event.model_validate(_event_record())  # 120 min > the 40-min budget
    cards = validate.template_cards([*places, ev_cand], intent, ctx,
                                    events={show.id: two_hours})
    got, _ = pipeline_mod._route(ctx, cards, [*places, ev_cand], intent, True, "rid")
    assert got is None or show.id not in got.stop_ids  # a card, not a stop


# ------------- the integrator's case: «концерт или шоу?» with a concert near EXPO in 15 min

SOON = {"id": "event:test-soon", "title": {"ru": "Тестовый концерт"}, "category": "entertainment",
        "tags": ["outdoor"], "geo": {"lat": 51.0892, "lon": 71.4162},
        "starts_at": "2026-10-22T15:15:00+05:00", "ends_at": "2026-10-22T16:30:00+05:00",
        "source_url": "https://example.org/a", "venue": "Тестовая площадка",
        "address": "пр. Мангилик Ел, 55/23", "fetched_at": "2026-10-21"}
SOON_Q = "Что интересного рядом — концерт или шоу?"


def test_a_soon_concert_is_a_card_and_the_last_stop(deps: Deps, tmp_path: Path) -> None:
    """The parser reads «что интересного» as sights and «концерт» as live_music: those are
    place filters. They hid the event (category entertainment); now the query that asks for
    events gets it — a card, and the last stop of the route (real plan_route of CORE)."""
    d = _events_deps(deps, tmp_path, [SOON])
    a = recommend(req(SOON_Q, use_llm=False, at="2026-10-22T15:00:00+05:00"), d)
    ev_cards = [c for c in a.cards if c.kind == "event"]
    assert [c.poi_id for c in ev_cards] == ["event:test-soon"]
    c = ev_cards[0]
    assert c.title == "Тестовый концерт" and c.venue == "Тестовая площадка"
    assert c.address == "пр. Мангилик Ел, 55/23" and c.facts_sources == ["manual"]
    assert a.route is not None and a.route.stop_ids[-1] == "event:test-soon"
    assert "события: без фильтра по категориям мест" in a.data_used.filters
    early = recommend(req(SOON_Q, use_llm=False, at="2026-10-22T14:00:00+05:00"), d)
    assert any(c.kind == "event" for c in early.cards)  # 75 min ahead: a card…
    assert early.route is None or "event:test-soon" not in early.route.stop_ids  # …not a stop
    over = recommend(req(SOON_Q, use_llm=False, at="2026-10-22T17:00:00+05:00"), d)
    assert not any(c.kind == "event" for c in over.cards)  # over at 16:30


def test_ui_categories_still_hide_events(deps: Deps, tmp_path: Path) -> None:
    d = _events_deps(deps, tmp_path, [SOON])
    a = recommend(req(SOON_Q, use_llm=False, at="2026-10-22T15:00:00+05:00",
                      overrides={"categories": ["museum"]}), d)
    assert not any(c.kind == "event" for c in a.cards)  # an explicit choice in the UI
    assert not any(f.startswith("события:") for f in a.data_used.filters)


def test_a_query_without_event_words_keeps_the_place_filters(deps: Deps, tmp_path: Path
                                                             ) -> None:
    d = _events_deps(deps, tmp_path, [SOON])
    a = recommend(req("музей рядом", use_llm=False, at="2026-10-22T15:00:00+05:00"), d)
    assert not any(c.kind == "event" for c in a.cards)  # an entertainment event, not a museum


def test_data_lane_records_load_as_they_are_written() -> None:
    """pipeline.events writes "event:<slug>" with its own slug rule: the reader accepts it."""
    import inspect
    from datetime import date

    from app import events as ev
    from pipeline import events as data_events

    item = data_events.EventIn.model_validate({
        "id": "opera-tosca-2026-11-01", "title": "Опера «Тоска»", "category": "theatre",
        "starts_at": "2026-11-01T19:00:00+05:00", "geo": {"lat": 51.1236, "lon": 71.4296},
        "source_url": "https://astanaopera.kz/afisha/tosca", "fetched_at": "2026-10-21"})
    venue = data_events.Venue(None, None, None)
    params = inspect.signature(data_events.to_record).parameters
    if "source" in params:  # DATA T132 passes the whole source (with its kind)
        fields = data_events.Source.model_fields
        src = {"domain": "astanaopera.kz", "name": "Астана Опера",
               "terms_url": "https://astanaopera.kz/terms", "checked_at": date(2026, 10, 21)}
        if "kind" in fields:
            src["kind"] = "venue"
        record = data_events.to_record(item, venue, data_events.Source.model_validate(src))
    else:
        record = data_events.to_record(item, venue, "Астана Опера")
    got = ev.Event.model_validate(record)
    assert got.id == "event:opera-tosca-2026-11-01" and got.title_in("ru") == "Опера «Тоска»"


# ---------- Megah: general queries do not hide events, an explicit category does (wave 8.1)

GENERAL_QUERIES = [
    ("ru", "Что интересного рядом"), ("ru", "Куда сходить"), ("ru", "Хочу необычное"),
    ("kk", "Жақын жерде не қызықты бар"), ("en", "What's interesting nearby"),
    ("en", "Where to go"),
]


@pytest.mark.parametrize(("lang", "query"), GENERAL_QUERIES)
def test_general_queries_keep_the_soon_event(deps: Deps, tmp_path: Path, lang: str,
                                             query: str) -> None:
    d = _events_deps(deps, tmp_path, [SOON])
    a = recommend(req(query, use_llm=False, lang=lang, at="2026-10-22T15:00:00+05:00"), d)
    assert "event:test-soon" in [c.poi_id for c in a.cards if c.kind == "event"]
    assert a.route is not None and a.route.stop_ids[-1] == "event:test-soon"  # real CORE


@pytest.mark.parametrize(("lang", "query"), [
    ("ru", "Хочу в музей"), ("kk", "Мұражайға барғым келеді"), ("en", "I want a museum")])
def test_an_explicit_category_keeps_only_matching_events(deps: Deps, tmp_path: Path,
                                                         lang: str, query: str) -> None:
    show = {**SOON, "id": "event:test-show", "title": {"ru": "Выставка-тест"},
            "category": "gallery", "geo": {"lat": 51.0890, "lon": 71.4160}}
    d = _events_deps(deps, tmp_path, [SOON, show])
    a = recommend(req(query, use_llm=False, lang=lang, at="2026-10-22T15:00:00+05:00"), d)
    shown = {c.poi_id for c in a.cards if c.kind == "event"}
    assert "event:test-soon" not in shown  # an entertainment event is not a museum
    assert "event:test-show" in shown  # an exhibition is (museum ~ gallery for events)


def test_explicit_categories_are_the_named_ones_only() -> None:
    from app.query_parser import explicit_categories

    assert explicit_categories("Что интересного рядом") == []
    assert explicit_categories("What's worth seeing nearby?") == []
    assert explicit_categories("Хочу в музей") == ["museum"]
    assert set(explicit_categories("где поесть")) >= {"restaurant", "cafe"}
    assert explicit_categories("kk бар ма", "kk") == []  # Kazakh «бар» = «there is»


def test_no_bars_in_likes_reads_as_avoid_bars(deps: Deps) -> None:
    """Contract 66a26ca: the 1.3.0 like «no_bars» is a synonym of avoids «bars» (no 422)."""
    late = "2026-10-19T20:00:00+05:00"  # the test bar is open
    a = recommend(req("куда сходить", use_llm=False, at=late, prefs={"likes": ["no_bars"]}),
                  deps)
    assert "избегать: бары" in a.data_used.filters
    assert not any(c.category in ("bar", "nightlife") for c in a.cards)


# ------------- wave 8.2: a long event (a fair 14:00–21:00) is not a 6-hour visit in the route

FAIR = {**SOON, "id": "event:test-fair", "title": {"ru": "Выставка-ярмарка"},
        "category": "gallery", "tags": ["indoor"],
        "starts_at": "2026-10-22T14:00:00+05:00", "ends_at": "2026-10-22T21:00:00+05:00"}


@pytest.mark.parametrize("use_llm", [False, True])
def test_a_long_event_gets_the_category_visit_and_the_route_fits(deps: Deps, tmp_path: Path,
                                                                 use_llm: bool) -> None:
    d = _events_deps(deps, tmp_path, [FAIR])
    at = "2026-10-22T15:00:00+05:00"  # on now: 6 h left of 7
    plain = recommend(req("Куда сходить рядом?", use_llm=use_llm, at=at), d)
    fair = next(c for c in plain.cards if c.poi_id == "event:test-fair")
    assert (fair.visit_min_low, fair.visit_min_high) == (30, 60)  # the gallery estimate
    assert plain.route is not None and plain.route.stop_ids[-1] == "event:test-fair"
    assert plain.route.total_min < 180  # not «≈362 мин с посещением»
    budget = recommend(req("Куда сходить рядом на 2 часа?", use_llm=use_llm, at=at), d)
    assert budget.intent.time_budget_min == 120
    assert budget.route is not None and budget.route.total_min <= 120
    assert "event:test-fair" in budget.route.stop_ids


def test_a_two_hour_concert_is_still_visited_whole(deps: Deps, tmp_path: Path) -> None:
    for category in ("theatre", "other"):  # whatever the category: up to 2 h stays whole
        rec = _event_record(category=category)
        a = recommend(req(EVENT_Q if category == "theatre" else MORE_Q, use_llm=False),
                      _events_deps(deps, tmp_path, [rec]))
        c = next(c for c in a.cards if c.kind == "event")
        assert (c.visit_min_low, c.visit_min_high) == (120, 120), category


# ---------------------- QADAM_EVENTS_PATH (settings.events_path): eval's own events file (E16)


def test_events_come_from_qadam_events_path(deps: Deps, tmp_path: Path,
                                            monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "eval_events.json"
    path.write_text(json.dumps([SOON], ensure_ascii=False), encoding="utf-8")
    monkeypatch.setenv("QADAM_EVENTS_PATH", str(path))
    get_settings.cache_clear()
    settings = dataclasses.replace(get_settings(), cache_dir=tmp_path / "cache",
                                   index_dir=tmp_path / "index", h3_res=9)
    assert settings.events_path == path
    d = Deps(store=deps.store, llm=deps.llm, settings=settings)
    a = recommend(req(SOON_Q, use_llm=False, at="2026-10-22T15:00:00+05:00"), d)
    assert "event:test-soon" in [c.poi_id for c in a.cards]

    monkeypatch.delenv("QADAM_EVENTS_PATH")
    data_dir = tmp_path / "data"  # not the repo's data/: the test must not depend on it
    monkeypatch.setenv("QADAM_DATA_DIR", str(data_dir))
    get_settings.cache_clear()
    default = get_settings()
    assert default.events_path == data_dir / "events.json"  # the default: data_dir/events.json
    b = recommend(req(SOON_Q, use_llm=False, at="2026-10-22T15:00:00+05:00"),
                  Deps(store=deps.store, llm=deps.llm, settings=dataclasses.replace(
                      default, cache_dir=tmp_path / "cache", index_dir=tmp_path / "index")))
    assert not any(c.kind == "event" for c in b.cards)  # no file there: no events


def test_no_events_path_means_no_events(deps: Deps) -> None:
    d = Deps(store=deps.store, llm=deps.llm,
             settings=dataclasses.replace(deps.settings, events_path=None))
    a = recommend(req(SOON_Q, use_llm=False, at="2026-10-22T15:00:00+05:00"), d)
    assert a.cards and not any(c.kind == "event" for c in a.cards)


@pytest.mark.parametrize("use_llm", [False, True])
def test_an_event_without_end_gets_the_category_visit_on_both_paths(
    deps: Deps, tmp_path: Path, use_llm: bool
) -> None:
    """No ends_at: the visit is the category estimate, never the model's guess (FakeLLM 20–30)."""
    d = _events_deps(deps, tmp_path, [_event_record(ends=None)])  # theatre
    a = recommend(req(MORE_Q, use_llm=use_llm), d)
    c = next(c for c in a.cards if c.kind == "event")
    assert a.data_used.generation == ("llm" if use_llm else "template")
    assert (c.visit_min_low, c.visit_min_high) == (90, 180)  # VISIT_MIN["theatre"]
