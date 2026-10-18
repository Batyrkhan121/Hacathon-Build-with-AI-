"""Query understanding: free text -> QueryIntent. Owner: LLM lane (task T31).

Two parsers, always both:
- parse_rules: deterministic keywords/regex for ru / kk / en (works offline, tested);
- LLM (prompts/parse_query.md, structured output, effort "low") refines it.
Merge: LLM result wins on fields it is confident about; rules fill gaps; a landmark is
accepted only if it exists in the gazetteer. The user text is DATA, never instructions.
"""

from __future__ import annotations

from app.llm import LLMClient
from app.schemas import Landmark, Lang, QueryIntent, UserOverrides


def detect_lang(text: str) -> Lang:
    """kk if Kazakh-specific letters (әғқңөұүһі) present, en if mostly Latin, else ru."""
    raise NotImplementedError("T31 (LLM)")


def parse_rules(text: str, lang_hint: Lang | None, landmarks: dict[str, Landmark]) -> QueryIntent:
    """Keywords -> categories / must_tags / with_kids / wheelchair / night / tourist /
    group_size; time: "30–60 минут", "1–2 часа", "полчаса", "пару часов", "2 hours", "2 сағат"."""
    raise NotImplementedError("T31 (LLM)")


def parse_query(
    text: str,
    lang_hint: Lang | None,
    llm: LLMClient | None,
    landmarks: dict[str, Landmark],
) -> QueryIntent:
    """LLM first (if available), rules as fallback and gap filler. Sets intent.parser."""
    raise NotImplementedError("T31 (LLM)")


def apply_overrides(intent: QueryIntent, overrides: UserOverrides | None) -> QueryIntent:
    """Explicit UI filters always win over the parsed intent."""
    raise NotImplementedError("T31 (LLM)")
