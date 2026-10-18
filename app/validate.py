"""Guardrails between the LLM and the user + deterministic fallback cards.

Owner: CORE lane (task T24). Pure functions, no network.
"""

from __future__ import annotations

from app.schemas import Candidate, Card, DraftResponse, QueryIntent, SearchContext

# Claims we never let through unless the candidate is open with high confidence.
FORBIDDEN_OPEN_CLAIMS: tuple[str, ...] = (
    "открыто сейчас",
    "сейчас открыт",
    "работает сейчас",
    "open now",
    "currently open",
    "қазір ашық",
)
MAX_WHY_CHARS = 280
MAX_TODO_CHARS = 360


def finalize(
    draft: DraftResponse,
    candidates: list[Candidate],
    intent: QueryIntent,
    ctx: SearchContext,
) -> tuple[list[Card], list[str]]:
    """Turn LLM drafts into Cards. Returns (cards, warnings).

    - drop drafts whose poi_id is not among candidates or is duplicated;
    - facts (title, category, geo, distance, walk, hours, sources, confidence, address)
      come from the Candidate, never from the draft;
    - strip FORBIDDEN_OPEN_CLAIMS unless hours.status == "open" and confidence == "high";
    - clamp visit minutes (low <= high), trim texts to limits, rank 1..3;
    - if fewer than 1 valid card remains -> return template_cards(...) and a warning.
    """
    raise NotImplementedError("T24 (CORE)")


def template_cards(
    candidates: list[Candidate],
    intent: QueryIntent,
    ctx: SearchContext,
    n: int = 3,
) -> list[Card]:
    """Deterministic cards without LLM (fallback and `use_llm=False`).

    why = joined Candidate.matched reasons; what_to_do = per-category template in intent.lang;
    idea_source = "poi_data". Prefer category diversity among the n cards.
    """
    raise NotImplementedError("T24 (CORE)")
