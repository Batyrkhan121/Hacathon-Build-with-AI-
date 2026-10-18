"""Guardrails between the LLM and the user + deterministic fallback cards.

Owner: CORE lane (task T24). Pure functions, no network.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Literal

from app.retrieval import explain, generic_key
from app.schemas import (
    POI,
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
    # hours claims the code did not confirm
    "now open",
    "already open",
    "open until",
    "open till",
    "открыто до",
    "открыт до",
    "открыта до",
    "работает до",
    "уже работает",
)
MAX_WHY_CHARS = 280
MAX_TODO_CHARS = 360
MAX_WHY_SENTENCES = 2
MAX_CARDS = 3
UNKNOWN_HOURS_NOTE = "нет данных о часах, проверьте перед визитом"
UNKNOWN_HOURS_NOTES: dict[str, str] = {  # T70 step 6: the note follows the user's language
    "ru": UNKNOWN_HOURS_NOTE,
    "kk": "жұмыс уақыты туралы дерек жоқ, барар алдында тексеріңіз",
    "en": "no opening hours data, check before you go",
}

_CLAIM_RE = re.compile(
    "|".join(r"\s+".join(map(re.escape, claim.split())) for claim in FORBIDDEN_OPEN_CLAIMS),
    re.IGNORECASE,
)
# sentence end, but not after common abbreviations: «ул. Абая», «пр. Мангилик Ел»
_SENTENCE_SPLIT = re.compile(
    r"(?<!\bул\.)(?<!\bпр\.)(?<!\bд\.)(?<!\bг\.)(?<!\bим\.)(?<!\bст\.)(?<!\bst\.)"
    r"(?<=[.!?…])\s+",
    re.IGNORECASE,
)

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


# T70 step 9: hours notes come from hours.py in Russian; cards for kk / en get them translated.
# Ordered: longer phrases first. Only the fixed vocabulary of app/hours.py is used there.
_NOTE_PHRASES: dict[str, tuple[tuple[str, str], ...]] = {
    "en": (
        ("по данным OSM сегодня не работает", "closed today according to OSM"),
        ("в праздники часы могут отличаться", "hours may differ on holidays"),
        ("откроется завтра в", "opens tomorrow at"),
        ("откроется через", "opens in"),
        ("откроется в", "opens at"),
        ("закроется через", "closes in"),
        ("по данным OSM:", "OSM hours:"),
        ("сегодня выходной", "closed today"),
        ("закрыто", "closed"),
        ("ежедневно", "daily"),
        ("круглосуточно", "24/7"),
        ("выходной", "closed"),
        ("часы:", "hours:"),
    ),
    "kk": (
        ("по данным OSM сегодня не работает", "OSM деректері бойынша бүгін жұмыс істемейді"),
        ("в праздники часы могут отличаться", "мереке күндері уақыт өзгеруі мүмкін"),
        ("откроется завтра в", "ертең ашылады:"),
        ("откроется через", "ашылуына"),
        ("откроется в", "ашылады:"),
        ("закроется через", "жабылуына"),
        ("по данным OSM:", "OSM деректері бойынша:"),
        ("сегодня выходной", "бүгін демалыс"),
        ("закрыто", "жабық"),
        ("ежедневно", "күн сайын"),
        ("круглосуточно", "тәулік бойы"),
        ("выходной", "демалыс"),
        ("часы:", "уақыты:"),
    ),
}
_NOTE_DAYS: dict[str, dict[str, str]] = {
    "en": {
        "Пн": "Mon", "Вт": "Tue", "Ср": "Wed", "Чт": "Thu", "Пт": "Fri", "Сб": "Sat", "Вс": "Sun"
    },
    "kk": {"Пн": "Дс", "Вт": "Сс", "Ср": "Ср", "Чт": "Бс", "Пт": "Жм", "Сб": "Сн", "Вс": "Жс"},
}
_NOTE_UNITS: dict[str, dict[str, str]] = {"en": {"ч": "h", "мин": "min"}, "kk": {"ч": "сағ"}}
_NOTE_DAY_RE = re.compile(r"\b(Пн|Вт|Ср|Чт|Пт|Сб|Вс)\b")
_NOTE_UNIT_RE = re.compile(r"(?<=\d )(ч|мин)\b")


def _localize_note(note: str, lang: Lang) -> str:
    """Translate an app/hours.py note for kk / en cards; ru notes are returned as is."""
    if lang not in _NOTE_PHRASES:
        return note
    for ru, other in _NOTE_PHRASES[lang]:
        note = note.replace(ru, other)
    days, units = _NOTE_DAYS[lang], _NOTE_UNITS[lang]
    note = _NOTE_DAY_RE.sub(lambda m: days[m.group(1)], note)
    return _NOTE_UNIT_RE.sub(lambda m: units.get(m.group(1), m.group(1)), note)


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
        # a route has up to 3 stops: the minimal visit must leave room for the others
        low = min(low, high, max(10, intent.time_budget_min // 3))
    return low, high


def _facts_sources(cand: Candidate) -> list[Source]:
    sources: list[Source] = list(dict.fromkeys(cand.poi.sources))
    facts: list[Source] = [s for s in sources if s != "llm_generated"]
    return facts or sources


def _card(
    rank: int,
    cand: Candidate,
    intent: QueryIntent,
    why: str,
    todo: str,
    visit: tuple[int, int],
    idea_source: Literal["poi_data", "generated"],
) -> Card:
    poi, hours = cand.poi, cand.hours
    note = hours.note
    if hours.status == "unknown" and (intent.lang != "ru" or "проверьте" not in note):
        # ru keeps a more specific hours.py note ("…нестандартный формат, проверьте…")
        note = UNKNOWN_HOURS_NOTES.get(intent.lang, UNKNOWN_HOURS_NOTE)
    else:
        note = _localize_note(note, intent.lang)
    return Card(
        rank=rank,
        poi_id=poi.id,
        title=_title(poi, intent.lang),
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


def _distance_label(metres: int, lang: Lang) -> str:
    if metres < 1000:
        return f"{metres} m" if lang == "en" else f"{metres} м"
    tenths = int(metres / 100 + 0.5)  # round half up: 1250 m -> 1,3 km
    sep = "." if lang == "en" else ","
    km = f"{tenths // 10}{sep}{tenths % 10}"
    return f"{km} km" if lang == "en" else f"{km} км"


# Unnamed OSM places carry a Russian generic name; kk / en cards show it translated.
GENERIC_TITLES: dict[str, dict[str, str]] = {
    "детская площадка": {"kk": "Балалар алаңы", "en": "Playground"},
    "детская игровая площадка": {"kk": "Балалар алаңы", "en": "Playground"},
    "игровая площадка": {"kk": "Ойын алаңы", "en": "Playground"},
    "площадка": {"kk": "Алаң", "en": "Playground"},
    "спортивная площадка": {"kk": "Спорт алаңы", "en": "Sports ground"},
    "сквер": {"kk": "Гүлзар", "en": "Public garden"},
    "парк": {"kk": "Саябақ", "en": "Park"},
    "кафе": {"kk": "Кафе", "en": "Cafe"},
    "кофейня": {"kk": "Кофехана", "en": "Coffee shop"},
    "ресторан": {"kk": "Мейрамхана", "en": "Restaurant"},
    "памятник": {"kk": "Ескерткіш", "en": "Monument"},
    "смотровая площадка": {"kk": "Көру алаңы", "en": "Viewpoint"},
    "фонтан": {"kk": "Субұрқақ", "en": "Fountain"},
}
_NEAR_NAME = re.compile(r"^(?P<generic>.+?)\s+у\s+(?P<near>.+)$")  # «Детская площадка у Хатико»


def _generic_name(name: str) -> str:
    return name.lower().replace("ё", "е").strip(" «»\"'")


def _title(poi: POI, lang: Lang) -> str:
    """Own translation first; unnamed places get a translated generic name for kk / en."""
    own = poi.name_i18n.get(lang)
    if own or lang == "ru":
        return own or poi.name
    generic = GENERIC_TITLES.get(_generic_name(poi.name))
    if generic:
        return generic[lang]
    near = _NEAR_NAME.match(poi.name)
    generic = GENERIC_TITLES.get(_generic_name(near["generic"])) if near else None
    if near and generic:
        if lang == "en":
            return f"{generic['en']} near {near['near']}"
        kk = generic["kk"]
        return f"{near['near']} маңындағы {kk[0].lower()}{kk[1:]}"
    return poi.name


def _distinct_titles(cards: list[Card], cands: list[Candidate], lang: Lang) -> list[Card]:
    """Unnamed places and same-named branches get the distance: «Детская площадка · 350 м».

    110 of 111 playgrounds in OSM are «Детская площадка», chains repeat (two «Drinkit»).
    If name and distance still collide, the address (or the rank) is appended.
    """
    counts = Counter(c.title.casefold() for c in cards)
    used: set[str] = set()
    out: list[Card] = []
    for card, cand in zip(cards, cands, strict=True):
        title = card.title
        unnamed = generic_key(cand.poi) is not None and not cand.poi.name_i18n.get(lang)
        if unnamed or counts[title.casefold()] > 1:
            title = f"{title} · {_distance_label(card.distance_m, lang)}"
        if title.casefold() in used:
            title = f"{title} · {cand.poi.address or f'№{card.rank}'}"
        used.add(title.casefold())
        out.append(card if title == card.title else card.model_copy(update={"title": title}))
    return out


# ------------------------------------------------------------------ public


def sanitize_summary(summary: str, cards: list[Card]) -> str:
    """Remove "open now" style sentences from the LLM summary unless every card is open/high.

    finalize() never sees DraftResponse.summary; the pipeline calls this on it.
    """
    if cards and all(c.open_status == "open" and c.open_confidence == "high" for c in cards):
        return summary.strip()
    return _strip_open_claims(summary)[0]


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
    - card titles are distinct: unnamed / same-named places get the distance;
    - if fewer than 1 valid card remains -> return template_cards(...) and a warning.
    """
    if not candidates:
        return [], ["Рядом не нашлось подходящих мест по заданным условиям"]

    by_id = {c.poi.id: c for c in candidates}
    cards: list[Card] = []
    seen: set[str] = set()  # poi ids and generic names ("playground:детская площадка")
    foreign = duplicates = stripped = 0
    for item in draft.cards:
        if item.poi_id not in by_id:
            foreign += 1
            continue
        cand = by_id[item.poi_id]
        generic = generic_key(cand.poi)
        if item.poi_id in seen or (generic is not None and generic in seen):
            duplicates += 1
            continue
        if len(cards) >= MAX_CARDS:
            break
        seen.add(item.poi_id)
        if generic is not None:
            seen.add(generic)

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
    return _distinct_titles(cards, [by_id[c.poi_id] for c in cards], intent.lang), warnings


def template_cards(
    candidates: list[Candidate],
    intent: QueryIntent,
    ctx: SearchContext,
    n: int = 3,
) -> list[Card]:
    """Deterministic cards without LLM (fallback and `use_llm=False`).

    why = joined Candidate.matched reasons; what_to_do = per-category template in intent.lang;
    idea_source = "poi_data". Prefer category diversity among the n cards; never two cards
    for unnamed places with the same generic name (e.g. «Детская площадка»).
    """
    n = max(0, min(n, MAX_CARDS))
    chosen: list[Candidate] = []
    for strict in (True, False):  # strict pass: at most 2 cards per category
        for cand in candidates:
            if len(chosen) >= n:
                break
            if any(c.poi.id == cand.poi.id for c in chosen):
                continue
            # never two cards for unnamed places with the same name: fewer cards is better
            key = generic_key(cand.poi)
            if key is not None and any(generic_key(c.poi) == key for c in chosen):
                continue
            if strict and sum(c.poi.category == cand.poi.category for c in chosen) >= 2:
                continue
            chosen.append(cand)
    cards = [
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
    return _distinct_titles(cards, chosen, intent.lang)
