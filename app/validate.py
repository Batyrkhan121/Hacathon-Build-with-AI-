"""Guardrails between the LLM and the user + deterministic fallback cards.

Owner: CORE lane (task T24). Pure functions, no network.
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from app import events as ev
from app.hours import unknown_note
from app.retrieval import EVENT_WORDS, explain, generic_key, is_event, poi_text, text_stems
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
    # hours claims the code did not confirm («открыто до 22», «open until …» are caught by
    # root + time marker in has_open_claim; plain substrings would hit «работает допоздна»)
    "now open",
    "already open",
    "уже работает",
)
MAX_WHY_CHARS = 280
MAX_TODO_CHARS = 360
MAX_WHY_SENTENCES = 2
MAX_CARDS = 3
MAX_SUMMARY_CHARS = 200
# finalize() warnings are shown to the user, so they follow intent.lang (product rule 6)
FINALIZE_WARNINGS: dict[str, dict[str, str]] = {
    "ru": {
        "no_candidates": "Рядом не нашлось подходящих мест по заданным условиям",
        "foreign": "Отброшено карточек ИИ с местами не из списка кандидатов: {n}",
        "duplicates": "Отброшено повторных карточек ИИ: {n}",
        "claims": "Убрано утверждение о текущей работе места: часы не подтверждены",
        "facts": "Убраны непроверяемые детали из текста ИИ: "
        "цены, телефоны, адреса или числа не из данных",
        "fallback": "ИИ не дал подходящих карточек, показаны карточки по данным",
    },
    "kk": {
        "no_candidates": "Берілген шарттар бойынша жақын жерде лайықты орын табылмады",
        "foreign": "Тізімде жоқ орындары бар ЖИ карточкалары алынып тасталды: {n}",
        "duplicates": "Қайталанған ЖИ карточкалары алынып тасталды: {n}",
        "claims": "Жұмыс уақыты туралы расталмаған тұжырым алынып тасталды",
        "facts": "ЖИ мәтінінен тексерілмейтін мәліметтер алынып тасталды: "
        "бағалар, телефондар, мекенжайлар немесе деректерде жоқ сандар",
        "fallback": "ЖИ лайықты карточка бермеді, деректер бойынша карточкалар көрсетілді",
    },
    "en": {
        "no_candidates": "No suitable places nearby for these conditions",
        "foreign": "Dropped AI cards with places outside the candidate list: {n}",
        "duplicates": "Dropped repeated AI cards: {n}",
        "claims": "Removed an unconfirmed statement about opening hours",
        "facts": "Removed unverifiable details from the AI text: "
        "prices, phones, addresses or numbers not in the data",
        "fallback": "The AI gave no usable cards, showing cards built from the data",
    },
}
UNKNOWN_HOURS_NOTE = unknown_note("ru")  # texts in ru/kk/en live in app.hours
EVENT_TODO: dict[str, str] = {
    "ru": "Проверьте время и билеты на странице источника и приходите заранее.",
    "kk": "Уақыты мен билеттерді дереккөз бетінен тексеріп, ертерек келіңіз.",
    "en": "Check the time and tickets on the source page and come a little early.",
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
    "indoor_fun": {
        "ru": "Загляните внутрь и выберите, что попробовать; заложите время с запасом.",
        "kk": "Ішке кіріп, нені көретініңізді таңдаңыз; уақытты қормен жоспарлаңыз.",
        "en": "Step inside and pick what to try; allow some extra time.",
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


# ------------------------------------------------------------- claim / fact guards
# RAG review (integrator, wave 3): the phrase list above is easy to bypass (plural forms,
# «в данный момент», «круглосуточно», kk «ашық», zero-width chars, Latin homoglyphs).
# A sentence is an "open now / hours" claim when an hours root meets a time marker, or when
# it is one of the standalone forms. Text is NFKC-normalised, zero-width characters are
# removed and mixed-script words are folded to one alphabet before matching.

_ZERO_WIDTH = dict.fromkeys(
    map(ord, "\u00ad\u200b\u200c\u200d\u200e\u200f\u2060\ufeff")  # soft hyphen, ZW*, BOM
)
_LAT_TO_CYR = str.maketrans("aceopxykmtABCEHKMOPTXY", "асеорхукмтАВСЕНКМОРТХУ")
_CYR_TO_LAT = str.maketrans("асеорхукмтАВСЕНКМОРТХУ", "aceopxykmtABCEHKMOPTXY")
_HAS_CYR = re.compile(r"[а-яёәіңғүұқөһ]", re.IGNORECASE)
_HAS_LAT = re.compile(r"[a-z]", re.IGNORECASE)
_WORDS = re.compile(r"\w+")

# «открытые данные / ашық деректер / open data» is about the data, not about the hours
_RU_ROOT = re.compile(r"\b(открыт\w*+(?!\s+данн)|работа(ет|ют|ть|ющ\w*))")
_RU_MARK = re.compile(
    r"(сейчас|в данный момент|в настоящее время|\bещ[её]\b|\bуже\b|сегодня|"
    r"\bдо\s+\d|до\s+(полуночи|утра|ночи|вечера)|24\s*/\s*7|24\s*час|"
    r"\bс\s+\d{1,2}(?:[:.]\d{2})?\s*(?:до|-|–|—)\s*\d)"  # «с 9 до 22», not «с 3 людьми»
)
# «открыто до закрытия / до позднего вечера», «работает до последнего посетителя»
_RU_UNTIL = re.compile(r"\b(открыт\w*+(?!\s+данн)|работа(ет|ют))\s+(\w+\s+)?до\b")
_RU_ALONE = re.compile(r"(круглосуточн\w*|24\s*/\s*7|без выходных)")
_KK_ROOT = re.compile(r"(ашық(?!\s+дерек)|жұмыс\s+істе\w*)")
_KK_MARK = re.compile(r"(қазір|\bәлі\b|бүгін|дейін|\d{1,2}\s*[:.]\s*\d{2}|тәулік)")
_KK_ALONE = re.compile(r"(жұмыс\s+істеп\s+тұр\w*|тәулік\s+бойы|ашық\s*[.!…]*\s*$)")
_EN_ROOT = re.compile(r"\b(open\w*+(?!\s+data)|operat\w*)\b")
_EN_MARK = re.compile(
    r"(\bnow\b|\bcurrently\b|\btoday\b|\btonight\b|\bstill\b|\balready\b|\buntil\b|\btill\b|"
    r"24\s*/\s*7|24 hours|at the moment)"
)
_EN_ALONE = re.compile(
    r"(\b(we[’']?re|we are|it[’']?s|it is|is|are|stays?)\s+open\b(?![-\s]air)|\bopen\s+24|"
    r"24\s*/\s*7|around the clock)"
)
# «круглосуточно / 24/7» is fine when the data says so (tag 24_7): checked with all_day_ok
_ALL_DAY = re.compile(
    r"(круглосуточн\w*|24\s*/\s*7|24\s*час\w*|тәулік\s+бойы|around the clock|24 hours)"
)
# T123: «идёт сейчас» about an event is a claim too: only when starts_at ≤ now < ends_at
_LIVE = re.compile(
    r"(ид[её]т\s+сейчас|сейчас\s+ид[её]т|уже\s+ид[её]т|уже\s+начал\w*|в\s+разгаре|"
    r"happening\s+now|\bon\s+now\b|already\s+(started|on)|under\s*way|"
    r"қазір\s+өт\w*|басталып\s+кетт\w*)"
)


def _fold(text: str, to_cyrillic: bool) -> str:
    """NFKC, no zero-width chars, mixed-script words folded to one alphabet, lower case.

    Only for matching: the text shown to the user keeps its own typography («…», «№»).
    """
    text = unicodedata.normalize("NFKC", text).translate(_ZERO_WIDTH)
    table, probe = (_LAT_TO_CYR, _HAS_CYR) if to_cyrillic else (_CYR_TO_LAT, _HAS_LAT)
    text = _WORDS.sub(lambda m: m[0].translate(table) if probe.search(m[0]) else m[0], text)
    return text.lower().replace("ё", "е")


def _sentence_claims_open(sentence: str, all_day_ok: bool = False) -> bool:
    cyr, lat = _fold(sentence, True), _fold(sentence, False)
    if all_day_ok:  # the data says 24/7: «круглосуточно» is a fact, not a claim
        cyr, lat = _ALL_DAY.sub(" ", cyr), _ALL_DAY.sub(" ", lat)
    return bool(
        _CLAIM_RE.search(cyr)
        or _CLAIM_RE.search(lat)
        or (_RU_ROOT.search(cyr) and _RU_MARK.search(cyr))
        or _RU_UNTIL.search(cyr)
        or _RU_ALONE.search(cyr)
        or (_KK_ROOT.search(cyr) and _KK_MARK.search(cyr))
        or _KK_ALONE.search(cyr.strip())
        or (_EN_ROOT.search(lat) and _EN_MARK.search(lat))
        or _EN_ALONE.search(lat)
    )


def _sentences(text: str) -> list[str]:
    """Sentences as written (only invisible zero-width characters are removed)."""
    clean = text.translate(_ZERO_WIDTH)
    return [s for s in _SENTENCE_SPLIT.split(clean.strip()) if s]


def has_live_claim(text: str) -> bool:
    """«Идёт сейчас» / «happening now» / «қазір өтіп жатыр» in any sentence (ru, kk, en)."""
    return any(_LIVE.search(_fold(s, True)) or _LIVE.search(_fold(s, False))
               for s in _sentences(text))


def has_open_claim(text: str, *, all_day_ok: bool = False) -> bool:
    """True when any sentence claims a place is open now / states hours (ru, kk, en).

    all_day_ok=True when the place has the 24_7 tag: «круглосуточно» is then data.
    Shared with eval/run_eval.py so the eval checks exactly what the guard removes.
    """
    return any(_sentence_claims_open(s, all_day_ok) for s in _sentences(text))


_MONEY = re.compile(r"(тенге|теңге|₸|\$|€|₽|\bруб|\btenge\b|\bkzt\b|\busd\b)", re.IGNORECASE)
_PHONE = re.compile(
    r"(\+\s?7[\d\s()-]{6,}|\b8\s?\(?7\d{2}\)?[\d\s-]{6,}|\b\d{3}[\s-]\d{2}[\s-]\d{2}\b)"
)
_URL = re.compile(r"(https?://|www\.|\b[\w-]+\.(kz|com|ru|org|net|info)\b)", re.IGNORECASE)
_ADDRESS = re.compile(
    r"(\b(ул|пр|просп|мкр|пер|бул)\.\s*\w|\b(street|avenue)\s+\w|\bд\.\s*\d)",
    re.IGNORECASE,
)
# «на улице Абая», «по проспекту Мангилик Ел» — but not «на улице холодно»
_STREET = re.compile(
    r"\b(?:[Уу]лиц[аеуыи]|[Пп]роспект\w?|[Пп]ереул\w+|[Бб]ульвар\w?|[Мм]икрорайон\w?|"
    r"көшесі\w*|даңғыл\w*)\s+[А-ЯЁӘІҢҒҮҰҚӨҺA-Z0-9«\"]"
)
_TIME = re.compile(r"\b([01]?\d|2[0-4])[:.]([0-5]\d)\b(?!\s*(?:км|km|м\b|m\b))")
# «круглосуточно / 24/7» needs the 24_7 tag or the hours note; «закрывается поздно /
# открывается рано» needs known hours. «открывается вид на город» is about the view.
_HOURS_VERB = re.compile(
    r"(закрыва\w*+|открыва\w*+(?!\s+(вид|панорам))|\bcloses?\b|\bclosing\b|"
    r"\bopens?\b(?!\s+(onto|up|to))|жабыл\w*|ашыл\w*)"
)
_HOURS_HINT = re.compile(
    r"(\d|рано|поздно|утр\w*|вечер\w*|ноч\w*|полноч\w*|\bearly\b|\blate\b|morning|"
    r"evening|night|midnight|ерте|\bкеш\b|таң)"
)
_NUMBER = re.compile(r"\d+(?:[.,]\d+)?")


@dataclass(frozen=True)
class _Facts:
    """Numbers and clock times a text may mention: they come from the data or the code."""

    numbers: frozenset[float]
    times: frozenset[str]
    distances: tuple[int, ...]
    all_day_ok: bool = False  # the data says 24/7 (tag 24_7 or the hours note)
    hours_known: bool = True  # hours status is not unknown

    def merge(self, other: _Facts) -> _Facts:
        return _Facts(self.numbers | other.numbers, self.times | other.times,
                      self.distances + other.distances,
                      self.all_day_ok and other.all_day_ok,
                      self.hours_known and other.hours_known)


def _clock(hours: str, minutes: str) -> str:
    return f"{int(hours):02d}:{minutes}"


def _numbers_in(text: str | None) -> set[float]:
    return {float(n.replace(",", ".")) for n in _NUMBER.findall(text or "")}


def _facts_for(cand: Candidate, visit: tuple[int, int], intent: QueryIntent | None,
               title: str = "") -> _Facts:
    poi, hours = cand.poi, cand.hours
    numbers = {float(x) for x in (cand.distance_m, cand.walk_min, cand.walk_min + 1,
                                  max(1, cand.walk_min - 1), *visit)}
    numbers.add(round(cand.distance_m / 1000, 1))
    for value in (hours.closes_in_min, hours.opens_in_min):
        if value is not None:
            numbers.add(float(value))
    if intent is not None:
        for value in (intent.time_budget_min, intent.max_walk_min, intent.group_size):
            if value:
                numbers.add(float(value))
        if intent.time_budget_min and intent.time_budget_min >= 60:
            numbers.update({float(intent.time_budget_min // 60), intent.time_budget_min / 60})
    if "24_7" in poi.tags:
        numbers.update({24.0, 7.0})
    for text in (poi.name, title, poi.address, hours.note):
        numbers |= _numbers_in(text)
    times = frozenset(_clock(h, m) for h, m in _TIME.findall(hours.note))
    all_day = "24_7" in poi.tags or bool(_ALL_DAY.search(_fold(hours.note, True)))
    return _Facts(frozenset(numbers), times, (cand.distance_m,), all_day,
                  hours.status != "unknown")


def _sentence_unverified(sentence: str, facts: _Facts) -> bool:
    """Prices, phones, links, street addresses or numbers / times not found in the data."""
    folded = unicodedata.normalize("NFKC", sentence)
    if _MONEY.search(folded) or _PHONE.search(folded) or _URL.search(folded):
        return True
    variants = (_fold(sentence, True), _fold(sentence, False))
    if not facts.all_day_ok and any(_ALL_DAY.search(v) for v in variants):
        return True
    if not facts.hours_known and any(
        _HOURS_VERB.search(v) and _HOURS_HINT.search(v) for v in variants
    ):
        return True
    if _ADDRESS.search(folded) or _STREET.search(folded):
        return True
    for h, m in _TIME.findall(folded):
        if _clock(h, m) not in facts.times:
            return True
    rest = _TIME.sub(" ", folded)
    for token in _NUMBER.findall(rest):
        value = float(token.replace(",", "."))
        if value in facts.numbers:
            continue
        if any(abs(value - d) <= max(50, 0.1 * d) for d in facts.distances):
            continue  # «около 500 м» for 509 m
        return True
    return False


def _guard_text(text: str, facts: _Facts, allow_open: bool, allow_live: bool = True
                ) -> tuple[str, bool, bool]:
    """(kept text, open claims removed?, unverifiable facts removed?) — whole sentences.
    allow_live=False: an event that is not on now — «идёт сейчас» is cut as a claim."""
    kept: list[str] = []
    claims = facts_cut = False
    for sentence in _sentences(text):
        if not allow_open and _sentence_claims_open(sentence, facts.all_day_ok):
            claims = True
            continue
        if not allow_live and has_live_claim(sentence):
            claims = True
            continue
        if _sentence_unverified(sentence, facts):
            facts_cut = True
            continue
        kept.append(sentence)
    return " ".join(kept), claims, facts_cut


# idea_source="poi_data" must be backed by the record: a description, or the text mentions
# the name / category / tags of the place (hypothesis from the review; tune on eval with Claude)
def _backed_by_data(text: str, poi: POI) -> bool:
    if (poi.description or "").strip():
        return True
    stems = text_stems(poi_text(poi))
    if is_event(poi):  # the search words of an event («концерт», «афиша») are not its data
        stems -= text_stems(EVENT_WORDS)
    return bool(text_stems(text) & stems)


# ------------------------------------------------------------------ helpers


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


def _template_why(cand: Candidate, intent: QueryIntent, ctx: SearchContext,
                  event: ev.Event | None = None) -> str:
    if intent.lang == "ru" and cand.matched:
        reasons = list(cand.matched)
    else:
        reasons = explain(cand, intent, ctx, lang=intent.lang)
    if event is not None:  # the event time right after the walk: «≈5 мин пешком, начало в 19:00»
        reasons.insert(1, ev.reason(event, ctx.local_time, intent.lang))
    return _trim(_sentence(reasons[:3]), MAX_WHY_CHARS)


def _template_todo(poi: POI, lang: Lang) -> str:
    group = _TODO_GROUP.get(poi.category, "other")
    if group == "outdoor" and poi.indoor is True:  # rides inside Khan Shatyr, indoor attractions
        group = "indoor_fun"
    texts = TODO_TEMPLATES[group]
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
    event: ev.Event | None = None,
) -> Card:
    poi, hours = cand.poi, cand.hours
    if event is not None:
        return _event_card(rank, cand, intent, why, todo, visit, idea_source, event)
    note = hours.note  # already in the user's language (retrieval asks app.hours with lang)
    if hours.status == "unknown" and not (poi.opening_hours or "").strip():
        # same rule as pipeline._localize_hours, so its re-wording gives the same text
        note = unknown_note(intent.lang)
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


def _event_card(
    rank: int,
    cand: Candidate,
    intent: QueryIntent,
    why: str,
    todo: str,
    visit: tuple[int, int],
    idea_source: Literal["poi_data", "generated"],
    event: ev.Event,
) -> Card:
    """kind="event" (contract 1.3.0): poi_id = event id, title in the answer language,
    address = venue and address, facts_sources ["manual"], starts_at / ends_at / source_url
    from data/events.json; open_status unknown / low with the event time as the note (code)."""
    return Card(
        rank=rank,
        poi_id=event.id,
        title=event.title_in(intent.lang),
        category=event.category,
        geo=event.geo,
        distance_m=cand.distance_m,
        walk_min=cand.walk_min,
        visit_min_low=visit[0],
        visit_min_high=visit[1],
        why=why,
        what_to_do=todo,
        open_status="unknown",
        open_confidence="low",
        open_note=cand.hours.note,  # app.events.note: «Начало в 19:00 — по данным источника, …»
        idea_source=idea_source,
        facts_sources=["manual"],
        data_confidence=cand.poi.data_confidence,
        tags=list(cand.poi.tags),
        address=event.place_line(),
        website=None,
        kind="event",
        starts_at=event.starts_at,
        ends_at=event.ends_at,
        source_url=event.source_url,
    )


def _event_todo(lang: Lang) -> str:
    return EVENT_TODO.get(lang, EVENT_TODO["ru"])


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


def _facts_from_card(card: Card) -> _Facts:
    numbers = {float(x) for x in (card.distance_m, card.walk_min, card.visit_min_low,
                                  card.visit_min_high)}
    numbers.add(round(card.distance_m / 1000, 1))
    for text in (card.title, card.address, card.open_note):
        numbers |= _numbers_in(text)
    times = frozenset(_clock(h, m) for h, m in _TIME.findall(card.open_note))
    all_day = "24_7" in card.tags or bool(_ALL_DAY.search(_fold(card.open_note, True)))
    return _Facts(frozenset(numbers), times, (card.distance_m,), all_day,
                  card.open_status != "unknown")


# ------------------------------------------------------------------ public


def sanitize_summary(summary: str, cards: list[Card], *, llm: bool = False) -> str:
    """Remove "open now" style sentences unless every card is open/high.

    finalize() never sees DraftResponse.summary; the pipeline calls this on it.
    llm=True (the summary was written by the model): also drop sentences with prices, phones,
    links, addresses or numbers / times not found on the cards, and keep one short sentence.
    The pipeline's own template summaries (counts, airport times) use the default llm=False.
    """
    all_open = bool(cards) and all(
        c.open_status == "open" and c.open_confidence == "high" for c in cards
    )
    if not llm:
        sentences = _sentences(summary)
        kept = [s for s in sentences if all_open or not _sentence_claims_open(s)]
        return summary if len(kept) == len(sentences) else " ".join(kept)
    facts = _Facts(frozenset({float(len(cards))}), frozenset(), (), True, True)
    for card in cards:
        facts = facts.merge(_facts_from_card(card))
    text = _guard_text(summary, facts, allow_open=all_open)[0]
    return _trim(_first_sentences(text, 1), MAX_SUMMARY_CHARS)


def finalize(
    draft: DraftResponse,
    candidates: list[Candidate],
    intent: QueryIntent,
    ctx: SearchContext,
    *,
    events: Mapping[str, ev.Event] | None = None,
) -> tuple[list[Card], list[str]]:
    """Turn LLM drafts into Cards. Returns (cards, warnings).

    - drop drafts whose poi_id is not among candidates or is duplicated;
    - facts (title, category, geo, distance, walk, hours, sources, confidence, address)
      come from the Candidate, never from the draft;
    - strip FORBIDDEN_OPEN_CLAIMS unless hours.status == "open" and confidence == "high";
      (has_open_claim: roots + time markers, ru/kk/en, NFKC / zero-width / homoglyphs);
    - strip sentences with prices, phones, links, addresses, numbers or times not in data;
    - idea_source=poi_data only when the text is backed by the record, else generated;
    - clamp visit minutes (low <= high), trim texts to limits, rank 1..3;
    - card titles are distinct: unnamed / same-named places get the distance;
    - if fewer than 1 valid card remains -> return template_cards(...) and a warning.
    T123 `events` (id -> app.events.Event): a candidate that is an event becomes a
    kind="event" card; «идёт сейчас» is cut unless starts_at ≤ now < ends_at.
    """
    events = events or {}
    if not candidates:
        return [], [FINALIZE_WARNINGS.get(intent.lang, FINALIZE_WARNINGS["ru"])["no_candidates"]]

    by_id = {c.poi.id: c for c in candidates}
    cards: list[Card] = []
    seen: set[str] = set()  # poi ids and generic names ("playground:детская площадка")
    foreign = duplicates = stripped = facts_cut = 0
    for item in draft.cards:
        poi_id = item.poi_id.strip()
        if poi_id not in by_id:
            foreign += 1
            continue
        cand = by_id[poi_id]
        generic = generic_key(cand.poi)
        if poi_id in seen or (generic is not None and generic in seen):
            duplicates += 1
            continue
        if len(cards) >= MAX_CARDS:
            break
        seen.add(poi_id)
        if generic is not None:
            seen.add(generic)

        low, high = sorted((item.visit_min_low, item.visit_min_high))
        facts = _facts_for(cand, (low, high), intent, _title(cand.poi, intent.lang))
        allow_open = cand.hours.status == "open" and cand.hours.confidence == "high"
        event = events.get(poi_id)
        live = event is None or ev.is_live(event, ctx.local_time)
        why, why_claim, why_facts = _guard_text(item.why, facts, allow_open, live)
        todo, todo_claim, todo_facts = _guard_text(item.what_to_do, facts, allow_open, live)
        stripped += why_claim or todo_claim
        facts_cut += why_facts or todo_facts
        why = _trim(_first_sentences(why, MAX_WHY_SENTENCES), MAX_WHY_CHARS)
        todo = _trim(todo, MAX_TODO_CHARS)
        idea_source = item.idea_source
        if idea_source == "poi_data" and not _backed_by_data(f"{why} {todo}", cand.poi):
            idea_source = "generated"  # the model called its own idea "data"
        cards.append(
            _card(
                rank=len(cards) + 1,
                cand=cand,
                intent=intent,
                why=why or _template_why(cand, intent, ctx, event),
                todo=todo or (_event_todo(intent.lang) if event
                              else _template_todo(cand.poi, intent.lang)),
                visit=(low, high),
                idea_source=idea_source,
                event=event,
            )
        )

    text = FINALIZE_WARNINGS.get(intent.lang, FINALIZE_WARNINGS["ru"])
    warnings: list[str] = []
    if foreign:
        warnings.append(text["foreign"].format(n=foreign))
    if duplicates:
        warnings.append(text["duplicates"].format(n=duplicates))
    if stripped:
        warnings.append(text["claims"])
    if facts_cut:
        warnings.append(text["facts"])
    if not cards:
        warnings.append(text["fallback"])
        return template_cards(candidates, intent, ctx, events=events), warnings
    return _distinct_titles(cards, [by_id[c.poi_id] for c in cards], intent.lang), warnings


def template_cards(
    candidates: list[Candidate],
    intent: QueryIntent,
    ctx: SearchContext,
    n: int = 3,
    *,
    events: Mapping[str, ev.Event] | None = None,
) -> list[Card]:
    """Deterministic cards without LLM (fallback and `use_llm=False`).

    why = joined Candidate.matched reasons; what_to_do = per-category template in intent.lang;
    idea_source = "poi_data". Prefer category diversity among the n cards; never two cards
    for unnamed places with the same generic name (e.g. «Детская площадка»).
    T123 `events`: an event candidate becomes a kind="event" card (time from app.events).
    """
    events = events or {}
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
    cards = [_template_card(rank, cand, intent, ctx, events.get(cand.poi.id))
             for rank, cand in enumerate(chosen, start=1)]
    return _distinct_titles(cards, chosen, intent.lang)


def _template_card(rank: int, cand: Candidate, intent: QueryIntent, ctx: SearchContext,
                   event: ev.Event | None) -> Card:
    if event is None:
        return _card(rank=rank, cand=cand, intent=intent,
                     why=_template_why(cand, intent, ctx),
                     todo=_template_todo(cand.poi, intent.lang),
                     visit=_visit_minutes(cand.poi.category, intent), idea_source="poi_data")
    visit = ev.visit_minutes(event, ctx.local_time) or _visit_minutes(event.category, intent)
    return _card(rank=rank, cand=cand, intent=intent,
                 why=_template_why(cand, intent, ctx, event), todo=_event_todo(intent.lang),
                 visit=visit, idea_source="poi_data", event=event)
