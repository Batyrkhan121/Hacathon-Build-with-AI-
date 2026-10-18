"""End-to-end recommendation flow. Owner: LLM lane (task T34).

recommend():
 1. resolve_origin(location)                       app.geo       (CORE)
 2. parse_query + apply_overrides                  app.query_parser
 3. context: local time, radius_for, weather       app.hours / app.geo / app.weather
 4. query_vec = llm.embed_query(...) or None
 5. retrieve(store, intent, ctx, query_vec, k=8)   app.retrieval (CORE)
    no candidates -> widen radius x1.5 once -> still none -> empty answer + helpful warning
 6. draft_cards (LLM) -> finalize                  app.generate + app.validate (CORE)
    LLMUnavailable / use_llm=False -> template_cards
 7. plan_route for >= 2 cards when want_route      app.route (CORE)
 8. DataUsed + warnings + disclaimer -> Answer
Every step degrades gracefully; never raise to the API except for invalid input.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.config import Settings
from app.llm import LLMClient
from app.schemas import Answer, RecommendRequest
from app.store import Store

DISCLAIMER_RU = (
    "Подсказки основаны на открытых данных (OpenStreetMap и др.) и могут устареть. "
    "Часы работы и доступность проверяйте перед визитом. Тексты «Идея ИИ» сгенерированы моделью."
)


@dataclass
class Deps:
    store: Store
    llm: LLMClient | None
    settings: Settings


def recommend(req: RecommendRequest, deps: Deps) -> Answer:
    raise NotImplementedError("T34 (LLM)")
