"""Retrieval: hard filters -> scoring (semantic or lexical) -> rerank + diversity.

Owner: CORE lane (task T22). Deterministic, no network. See docs/ARCHITECTURE.md §RAG.

Starting weights (tune in Wave 3 by eval, keep them in WEIGHTS):
  score = 0.45*relevance + 0.20*proximity + 0.15*open_score + 0.10*popularity
        + 0.05*data_confidence + 0.05*weather_fit
relevance = cosine(query_vec, poi_vec) if embeddings else lexical overlap.
"""

from __future__ import annotations

import numpy as np

from app.schemas import Candidate, QueryIntent, SearchContext
from app.store import Store

WEIGHTS: dict[str, float] = {
    "relevance": 0.45,
    "proximity": 0.20,
    "open": 0.15,
    "popularity": 0.10,
    "confidence": 0.05,
    "weather": 0.05,
}
MMR_LAMBDA = 0.7


def retrieve(
    store: Store,
    intent: QueryIntent,
    ctx: SearchContext,
    query_vec: np.ndarray | None,
    k: int = 8,
) -> list[Candidate]:
    """Return up to k candidates sorted by final score (best first).

    Hard filters (in this order): distance <= ctx.radius_m; categories (if any);
    with_kids / wheelchair / indoor constraints; hours: drop places closed now with
    high confidence or closing before a minimal visit; night -> late_open / 24_7 / night only.
    Unknown hours are allowed but penalised. Fill Candidate.matched with short
    deterministic reasons in Russian (e.g. "есть кофе", "≈6 мин пешком").
    """
    raise NotImplementedError("T22 (CORE)")


def lexical_relevance(intent: QueryIntent, text: str) -> float:
    """0..1 keyword overlap between intent (raw_query + tags + categories) and POI text."""
    raise NotImplementedError("T22 (CORE)")
