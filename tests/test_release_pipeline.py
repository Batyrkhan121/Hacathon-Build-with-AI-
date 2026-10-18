"""Regression gates for the submission fixes; real core, no external services."""

from datetime import datetime
from pathlib import Path

import pytest

from app import pipeline, weather
from app.pipeline import Deps, recommend
from app.query_parser import departure_minutes
from app.schemas import RecommendRequest
from app.store import load_store

from .test_pipeline import make_settings


@pytest.fixture
def release_deps(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Deps:
    monkeypatch.setattr(weather, "get_weather", lambda *args: None)
    settings = make_settings(tmp_path)
    return Deps(store=load_store(settings), llm=None, settings=settings)


@pytest.mark.parametrize(("text", "minutes"), [
    ("хочу погулять 30 минут, рейс через 3 часа", 180),
    ("до вылета 2 часа, хочу кофе на 20 минут", 120),
    ("30 minute walk, my flight is in 3 hours", 180),
    ("two hours before my flight", 120),
    ("ұшуға 2 сағат қалды", 120),
    ("прогулка 30 минут, вечером рейс", None),
    ("30 минут перед вылетом хочу выпить кофе", 30),
    ("до вылета 15 часов", 900),
])
def test_departure_time_is_bound_to_flight(text: str, minutes: int | None) -> None:
    assert departure_minutes(text) == minutes


@pytest.mark.parametrize("query", [
    "хочу погулять 30 минут, рейс через 3 часа",
    "рейс через 3 часа, хочу погулять 30 минут",
])
def test_separate_walk_budget_and_flight_time(release_deps: Deps, query: str) -> None:
    answer = recommend(RecommendRequest(
        query=query,
        location={"landmark_id": "baiterek"},
        at=datetime.fromisoformat("2026-10-18T15:00:00+05:00"),
        use_llm=False,
    ), release_deps)
    assert answer.cards
    assert any("До вылета 180 мин" in warning for warning in answer.warnings)
    assert all(c.walk_min + c.visit_min_high <= 30 for c in answer.cards)
    assert not any("не позже" in warning for warning in answer.warnings)


@pytest.mark.parametrize(("query", "lang", "phrase"), [
    ("ұшуға 2 сағат қалды", "kk", "Ұшуға 120 мин"),
    ("departure in 2 hours", "en", "120 min to departure"),
])
def test_flight_time_does_not_require_tourist_keyword(
    release_deps: Deps, query: str, lang: str, phrase: str,
) -> None:
    answer = recommend(RecommendRequest(
        query=query, lang=lang, location={"landmark_id": "baiterek"}, use_llm=False,
        at=datetime.fromisoformat("2026-10-18T15:00:00+05:00"),
    ), release_deps)
    assert any(phrase in warning for warning in answer.warnings)
    assert all(card.walk_min + card.visit_min_high <= 25 for card in answer.cards)


def test_explicit_short_walk_survives_night_widening(release_deps: Deps) -> None:
    answer = recommend(RecommendRequest(
        query="кофе ночью", location={"landmark_id": "khan_shatyr"},
        at=datetime.fromisoformat("2026-10-18T23:30:00+05:00"),
        overrides={"max_walk_min": 1}, use_llm=False,
    ), release_deps)
    assert all(card.walk_min <= 1 for card in answer.cards)


def test_summary_has_fallback_when_sanitization_removes_everything(
    release_deps: Deps, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pipeline.validate, "sanitize_summary", lambda *args: "")
    answer = recommend(RecommendRequest(
        query="кофе", location={"landmark_id": "expo"}, use_llm=False,
        at=datetime.fromisoformat("2026-10-18T15:00:00+05:00"),
    ), release_deps)
    assert answer.summary.strip()


def test_departure_override_keeps_actual_minutes(release_deps: Deps) -> None:
    answer = recommend(RecommendRequest(
        query="кофе", location={"landmark_id": "expo"}, use_llm=False,
        at=datetime.fromisoformat("2026-10-18T15:00:00+05:00"),
        overrides={"departure_in_min": 900, "time_budget_min": 30},
    ), release_deps)
    assert any("До вылета 900 мин" in w for w in answer.warnings)
    assert all(c.walk_min + c.visit_min_high <= 30 for c in answer.cards)


def test_loop_alternatives_include_return_walk_in_visit_range(release_deps: Deps) -> None:
    answer = recommend(RecommendRequest(
        query="хочу прогулку на 60 минут", location={"landmark_id": "mega_silk_way"},
        at=datetime.fromisoformat("2026-10-18T15:00:00+05:00"), use_llm=False,
    ), release_deps)
    assert answer.cards and answer.route and answer.route.fits_budget
    assert all(2 * card.walk_min + card.visit_min_high <= 60 for card in answer.cards)


def test_unknown_requested_amenities_are_explicit(release_deps: Deps) -> None:
    answer = recommend(RecommendRequest(
        query="тихо кофе розетки", location={"landmark_id": "expo"},
        at=datetime.fromisoformat("2026-10-18T15:00:00+05:00"), use_llm=False,
    ), release_deps)
    assert answer.cards
    assert any("не подтверждены" in w and "розетки" in w and "тихо" in w
               for w in answer.warnings)
