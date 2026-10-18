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
from app import pipeline as pipeline_mod
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
