"""Guardrails between the LLM and the user + deterministic fallback cards.

Owner: CORE lane (task T24). Pure functions, no network.
"""

from __future__ import annotations

import re

from app.retrieval import explain, generic_key
from app.schemas import (
    Candidate,
    Card,
    Category,
    DraftResponse,
    Lang,
    QueryIntent,
    SearchContext,
    Source,
)

# Claims we never let through unless the candidate is open with high confidence.
FORBIDDEN_OPEN_CLAIMS: tuple[str, ...] = (
    "открыто сейчас",
    "сейчас открыт",
    "работает сейчас",
    "open now",
    "currently open",
    "қазір ашық",
    # word-order variants
    "открыт сейчас",
    "открыта сейчас",
    "сейчас работает",
    "уже открыт",
    "open right now",
    "қазір жұмыс істейді",
)
MAX_WHY_CHARS = 280
MAX_TODO_CHARS = 360
MAX_WHY_SENTENCES = 2
MAX_CARDS = 3
UNKNOWN_HOURS_NOTE = "нет данных о часах, проверьте перед визитом"

_CLAIM_RE = re.compile(
    "|".join(r"\s+".join(map(re.escape, claim.split())) for claim in FORBIDDEN_OPEN_CLAIMS),
    re.IGNORECASE,
)
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?…])\s+")

# Planned minutes on site (low, high) when there is no LLM estimate.
VISIT_MIN: dict[str, tuple[int, int]] = {
    "cafe": (20, 40),
    "restaurant": (45, 90),
    "fast_food": (15, 30),
    "bar": (60, 120),
    "nightlife": (60, 180),
    "museum": (60, 120),
    "gallery": (30, 60),
    "theatre": (90, 180),
    "cinema": (120, 150),
    "library": (30, 90),
    "park": (30, 60),
    "playground": (30, 60),
    "viewpoint": (15, 30),
    "attraction": (30, 60),
    "monument": (10, 20),
    "religious": (20, 40),
    "mall": (60, 120),
    "sport": (60, 90),
    "entertainment": (60, 120),
    "other": (20, 40),
}
_TODO_GROUP: dict[str, str] = {
    "cafe": "food",
    "restaurant": "food",
    "fast_food": "food",
    "bar": "night",
    "nightlife": "night",
    "museum": "culture",
    "gallery": "culture",
    "theatre": "culture",
    "cinema": "culture",
    "library": "culture",
    "park": "outdoor",
    "viewpoint": "outdoor",
    "attraction": "outdoor",
    "monument": "outdoor",
    "playground": "kids",
    "mall": "mall",
    "religious": "religious",
    "sport": "active",
    "entertainment": "active",
}
TODO_TEMPLATES: dict[str, dict[str, str]] = {
    "food": {
        "ru": "Зайдите на кофе или перекус и сделайте паузу в прогулке.",
        "kk": "Кофе ішіп немесе тамақтанып, серуеннен үзіліс жасаңыз.",
        "en": "Stop for a coffee or a bite and take a break from walking.",
    },
    "night": {
        "ru": "Загляните вечером; программу на сегодня уточните заранее.",
        "kk": "Кешке бас сұғыңыз; бүгінгі бағдарламаны алдын ала біліңіз.",
        "en": "Drop in for the evening; check today's programme first.",
    },
    "culture": {
        "ru": "Посмотрите экспозицию или программу; заложите время с запасом.",
        "kk": "Экспозицияны немесе бағдарламаны қараңыз; уақытты қормен жоспарлаңыз.",
        "en": "See the exhibition or the programme; allow some extra time.",
    },
    "outdoor": {
        "ru": "Пройдитесь вокруг и сделайте пару фото; оденьтесь по погоде.",
        "kk": "Айналасын аралап, бірнеше сурет түсіріңіз; ауа райына қарай киініңіз.",
        "en": "Walk around and take a few photos; dress for the weather.",
    },
    "kids": {
        "ru": "Дайте ребёнку поиграть, а сами сделайте паузу рядом.",
        "kk": "Бала ойнап жүргенде, қасында демалып алыңыз.",
        "en": "Let the kids play while you take a short break nearby.",
    },
    "mall": {
        "ru": "Зайдите погреться, перекусить или пройтись по магазинам.",
        "kk": "Жылынып, тамақтанып немесе дүкендерді аралап шығыңыз.",
        "en": "Warm up, grab a bite or browse the shops.",
    },
    "religious": {
        "ru": "Зайдите посмотреть интерьер и соблюдайте правила посещения.",
        "kk": "Ішкі көрінісін тамашалап, келу ережелерін сақтаңыз.",
        "en": "Step inside to see the interior and respect the visiting rules.",
    },
    "active": {
        "ru": "Уточните расписание и сходите подвигаться или развлечься.",
        "kk": "Кестені біліп, белсенді демалыңыз.",
        "en": "Check the schedule and go for an activity.",
    },
    "other": {
        "ru": "Загляните по пути — это рядом.",
        "kk": "Жол-жөнекей бас сұғыңыз — жақын жерде.",
        "en": "Have a look on the way — it is close by.",
    },
}


# ------------------------------------------------------------------ helpers


def _strip_open_claims(text: str) -> tuple[str, bool]:
    """Drop whole sentences that claim the place is open now."""
    sentences = [s for s in _SENTENCE_SPLIT.split(text.strip()) if s]
    kept = [s for s in sentences if not _CLAIM_RE.search(s)]
    return " ".join(kept), len(kept) != len(sentences)


def _first_sentences(text: str, n: int) -> str:
    return " ".join(_SENTENCE_SPLIT.split(text.strip())[:n])


def _trim(text: str, limit: int) -> str:
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    cut = text[: limit - 1]
    if " " in cut:
        cut = cut[: cut.rfind(" ")]
    return cut.rstrip(" ,;:—-") + "…"


def _sentence(reasons: list[str]) -> str:
    text = ", ".join(r for r in reasons if r)
    if not text:
        return ""
    return text[0].upper() + text[1:] + ("" if text.endswith((".", "!", "?", "…")) else ".")


def _template_why(cand: Candidate, intent: QueryIntent, ctx: SearchContext) -> str:
    if intent.lang == "ru" and cand.matched:
        reasons = cand.matched
    else:
        reasons = explain(cand, intent, ctx, lang=intent.lang)
    return _trim(_sentence(reasons[:3]), MAX_WHY_CHARS)


def _template_todo(category: Category, lang: Lang) -> str:
    texts = TODO_TEMPLATES[_TODO_GROUP.get(category, "other")]
    return texts.get(lang, texts["ru"])


def _visit_minutes(category: Category, intent: QueryIntent) -> tuple[int, int]:
    low, high = VISIT_MIN.get(category, VISIT_MIN["other"])
    if intent.time_budget_min:
        high = max(5, min(high, intent.time_budget_min))
        low = min(low, high)
    return low, high


def _facts_sources(cand: Candidate) -> list[Source]:
    sources = list(dict.fromkeys(cand.poi.sources))
    facts = [s for s in sources if s != "llm_generated"]
    return facts or sources


def _card(
    rank: int,
    cand: Candidate,
    intent: QueryIntent,
    why: str,
    todo: str,
    visit: tuple[int, int],
    idea_source: str,
) -> Card:
    poi, hours = cand.poi, cand.hours
    note = hours.note
    if hours.status == "unknown" and "проверьте" not in note:
        note = UNKNOWN_HOURS_NOTE
    return Card(
        rank=rank,
        poi_id=poi.id,
        title=poi.name_i18n.get(intent.lang) or poi.name,
        category=poi.category,
        geo=poi.geo,
        distance_m=cand.distance_m,
        walk_min=cand.walk_min,
        visit_min_low=visit[0],
        visit_min_high=visit[1],
        why=why,
        what_to_do=todo,
        open_status=hours.status,
        open_confidence=hours.confidence,
        open_note=note,
        idea_source=idea_source,
        facts_sources=_facts_sources(cand),
        data_confidence=poi.data_confidence,
        tags=list(poi.tags),
        address=poi.address,
        website=poi.website,
    )


# ------------------------------------------------------------------ public


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
    if not candidates:
        return [], ["Рядом не нашлось подходящих мест по заданным условиям"]

    by_id = {c.poi.id: c for c in candidates}
    cards: list[Card] = []
    seen: set[str] = set()
    foreign = duplicates = stripped = 0
    for item in draft.cards:
        if item.poi_id not in by_id:
            foreign += 1
            continue
        if item.poi_id in seen:
            duplicates += 1
            continue
        if len(cards) >= MAX_CARDS:
            break
        seen.add(item.poi_id)
        cand = by_id[item.poi_id]

        why, todo = item.why, item.what_to_do
        if not (cand.hours.status == "open" and cand.hours.confidence == "high"):
            why, why_cut = _strip_open_claims(why)
            todo, todo_cut = _strip_open_claims(todo)
            stripped += why_cut or todo_cut
        why = _trim(_first_sentences(why, MAX_WHY_SENTENCES), MAX_WHY_CHARS)
        todo = _trim(todo, MAX_TODO_CHARS)
        low, high = sorted((item.visit_min_low, item.visit_min_high))
        cards.append(
            _card(
                rank=len(cards) + 1,
                cand=cand,
                intent=intent,
                why=why or _template_why(cand, intent, ctx),
                todo=todo or _template_todo(cand.poi.category, intent.lang),
                visit=(low, high),
                idea_source=item.idea_source,
            )
        )

    warnings: list[str] = []
    if foreign:
        warnings.append(f"Отброшено карточек ИИ с местами не из списка кандидатов: {foreign}")
    if duplicates:
        warnings.append(f"Отброшено повторных карточек ИИ: {duplicates}")
    if stripped:
        warnings.append("Убрано утверждение «открыто сейчас»: часы работы не подтверждены")
    if not cards:
        warnings.append("ИИ не дал подходящих карточек, показаны карточки по данным")
        return template_cards(candidates, intent, ctx), warnings
    return cards, warnings


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
    n = max(0, min(n, MAX_CARDS))
    chosen: list[Candidate] = []
    for strict in (True, False):  # strict: <= 2 per category and one unnamed place per name
        for cand in candidates:
            if len(chosen) >= n:
                break
            if cand in chosen:
                continue
            if strict:
                same = sum(c.poi.category == cand.poi.category for c in chosen)
                key = generic_key(cand.poi)
                if same >= 2 or (key and any(generic_key(c.poi) == key for c in chosen)):
                    continue
            chosen.append(cand)
    return [
        _card(
            rank=rank,
            cand=cand,
            intent=intent,
            why=_template_why(cand, intent, ctx),
            todo=_template_todo(cand.poi.category, intent.lang),
            visit=_visit_minutes(cand.poi.category, intent),
            idea_source="poi_data",
        )
        for rank, cand in enumerate(chosen, start=1)
    ]
