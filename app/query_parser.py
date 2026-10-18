"""Query understanding: free text -> QueryIntent. Owner: LLM lane (task T31).

Two parsers, always both:
- parse_rules: deterministic keywords/regex for ru / kk / en (works offline, tested);
- LLM (prompts/parse_query.md, structured output, effort "low") refines it.
Merge: LLM result wins on fields it is confident about; rules fill gaps; a landmark is
accepted only if it exists in the gazetteer. The user text is DATA, never instructions.

Merge details (see `_merge`): LLM wins on numbers / categories / indoor when it set them;
boolean flags are OR-ed (keyword hits are high precision); tags are the union, filtered to
TAG_VOCAB; language: explicit hint > strong script signal (Kazakh letters, Latin) > LLM.
"""

from __future__ import annotations

import logging
import re
from typing import get_args

from app import geo
from app.llm import LLMClient, load_prompt, user_data_block
from app.schemas import TAG_VOCAB, Category, Landmark, Lang, QueryIntent, UserOverrides

log = logging.getLogger(__name__)

CATEGORIES: tuple[str, ...] = get_args(Category)
NEAR_WALK_MIN = 15  # "недалеко / рядом / nearby" without a time budget
GROUP_DEFAULT = 4  # "с друзьями / компания" without a number
SIGHTS = ["attraction", "monument", "museum", "viewpoint", "gallery", "religious"]
FOOD = ["cafe", "restaurant", "fast_food"]

_KK_LETTERS = frozenset("әғқңөұүһі")
_KK_WORDS = frozenset(
    {"бала", "баламен", "балам", "балалар", "балаға", "керек", "маған", "менің", "мен", "жер",
     "қайда", "бар", "ма", "ме", "болады", "сағат", "түнде", "серуен", "саябақ", "мұражай"}
)
_KK_WORDS_NEEDED = 2  # "мен" / "бар" alone also occur in Russian-ish text

# ------------------------------------------------------------------ time patterns

_NUM = r"(\d+(?:[.,]\d+)?)"
_U_MIN = r"(?:минут\w*|мин\.?|minutes?|mins?)"
_U_HOUR = r"(?:час\w*|ч\.?|hours?|hrs?|h|сағат\w*)"
_NOT_CLOCK = r"(?!\w)(?!\s*(?:ночи|утра|вечера|дня|am|pm)\b)"
_RANGE_RE = re.compile(rf"{_NUM}\s*(?:-|до|to)\s*{_NUM}\s*({_U_MIN}|{_U_HOUR}){_NOT_CLOCK}")
_SINGLE_RE = re.compile(rf"{_NUM}\s*({_U_MIN}|{_U_HOUR}){_NOT_CLOCK}")
_WALK_RES = (
    re.compile(rf"{_NUM}\s*{_U_MIN}\s*(?:пешком|ходьбы|ходу|walk\w*|on foot|жаяу)"),
    re.compile(rf"(?:не дальше|не больше|не более|в пределах|within)\s*{_NUM}\s*{_U_MIN}"),
    re.compile(rf"\bв\s+{_NUM}\s*минутах"),
)
_WORD_BUDGETS: tuple[tuple[re.Pattern[str], int], ...] = (
    (re.compile(r"полтора\s+час|час\w*[\s-]+полтора|one and a half hours?|an hour and a half"
                r"|бір жарым сағат"), 90),
    (re.compile(r"полчаса|пол\s+часа|half an hour|half-hour|жарты сағат"), 30),
    (re.compile(r"пар[уаы]\s+(?:свободных\s+)?час|пар[уаы]-тройк\w*\s+час"
                r"|couple (?:of )?(?:free |spare )?hours|\btwo (?:free |spare )?hours\b"
                r"|бір-екі сағат|екі сағат"), 120),
    (re.compile(r"\bthree (?:free |spare )?hours\b|үш сағат"), 180),
    (re.compile(r"полдня|half a day|жарты күн"), 240),
    (re.compile(r"весь день|all day|күні бойы"), 480),
    (re.compile(r"\bчас\b|\bчасок\b|\b(?:an|one) (?:free |spare )?hour\b|бір сағат"), 60),
)
_NEAR_RE = re.compile(r"недалеко|\bрядом\b|поблизости|\bблизко\b|nearby|near me|close by"
                      r"|close to me|жақын")

# ------------------------------------------------------------- flag patterns


def _rx(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern)


_KIDS_RE = _rx(r"реб[её]н|\bдет(?:и|ей|ям|ьми|ск\w*|вор\w*)|\bсын\w*|\bдоч[ьк]\w*|малыш"
               r"|\bбала(?!нс)\w*|\bkids?\b|\bchild\w*|toddler|\bson\b|daughter")
_WHEEL_RE = _rx(r"коляск|колясоч|инвалид|пандус|wheelchair|accessib|step-free|мүгедек|арбамен")
_NIGHT_RE = _rx(r"\bноч\w*|поздн\w*|после полуночи|\bтүн\w*|\bnight\w*|\blate\b|after midnight")
_TOURIST_RE = _rx(r"турист|вылет|\bрейс\w*|аэропорт|самол[её]т|пересадк|транзит|tourist"
                  r"|\bflight|airport|layover|first time in|ұшақ|әуежай")
_FLIGHT_RE = _rx(r"вылет|\bрейс\w*|аэропорт|самол[её]т|\bflight|airport|\bplane\b|ұшақ|әуежай"
                 r"|ұшу\w*|ұшам|ұшамын")
_DEPARTURE_RE = _rx(
    r"(?:до|перед)\s+(?:вылет|рейс|самолет|посадк)\w*"
    r"|(?:вылет|рейс|самолет)\w*\s+через"
    r"|before\s+(?:my\s+|the\s+|our\s+)?(?:flight|departure|plane)"
    r"|(?:flight|plane|departure)\s+(?:is\s+|leaves\s+)?in\s+(?:\d|an?\b|one|two|three)"
    r"|until\s+(?:my\s+|the\s+)?(?:flight|departure)"
    r"|(?:ұшуға|ұшаққа|рейске)\s+(?:дейін|\d)|(?:ұшуға|ұшаққа)\b.*қалды")
_INDOOR_RE = _rx(r"в помещени|внутри|в тепл|тепл\w* мест|под крыш|не на улиц|холодно|мороз"
                 r"|\bдожд|indoor|inside|warm place|somewhere warm|\bcold\b|\brain|жылы\b"
                 r"|ішінде|суық")
_OUTDOOR_RE = _rx(r"на улиц|на свеж\w* воздух|на воздух|outdoor|outside|open air|fresh air"
                  r"|таза ауа|далада")

_TAG_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("coffee", _rx(r"кофе|кофейн|капучин|латте|эспрессо|coffee|espresso|latte|cappuccino")),
    ("power_outlets", _rx(r"розетк|заряд|outlet|socket|\bcharg|plug in")),
    ("wifi", _rx(r"wi-?fi|вай-?фай|интернет")),
    ("work_friendly", _rx(r"поработ|работать|ноутбук|laptop|\bwork\b|working|удал[её]нк"
                          r"|жұмыс істе")),
    ("quiet", _rx(r"\bтих\w*|тишин|спокойн|без шума|не шумн|уедин|тыныш|quiet|\bcalm|peaceful")),
    ("interactive", _rx(r"интерактив|interactive|hands-on|потрогать|своими руками")),
    ("view", _rx(r"\bвид\b|\bвидом\b|панорам|смотров|\bviews?\b|skyline|көрініс")),
    ("photo_spot", _rx(r"фото|сфотограф|инстаграм|instagram|photo|сурет")),
    # "free hour / free time" is about time, not price
    ("free", _rx(r"бесплатн|без денег|\bfree\b(?!\s+(?:hours?|time|afternoon|evening|morning"
                 r"|day))|for free|тегін")),
    ("architecture", _rx(r"архитектур|architecture|сәулет")),
    ("history", _rx(r"истори|history|historic|тарих")),
    ("art", _rx(r"искусств|\bарт\b|\bart\b|картин|өнер|галере|gallery")),
    ("vegetarian", _rx(r"вегетариан|веган|vegan|vegetarian")),
    ("local_cuisine", _rx(r"(?:казахск|национальн|местн)\w* кухн|kazakh (?:food|cuisine)"
                          r"|local (?:food|cuisine)|бешбармак|қазақ тағам|ұлттық тағам")),
    ("live_music", _rx(r"жив\w* музык|live music|концерт|джаз|jazz|concert")),
    ("water", _rx(r"набережн|\bрек[аеиу]\b|ишим|есиль|есіл|\bwater\b|river|embankment|өзен")),
    ("walk", _rx(r"прогул|погулять|пройтись|\bwalk|stroll|серуен")),
    ("food", _rx(r"поесть|перекус|покушать|\bеда\b|\bfood\b|\beat\b|тамақ")),
    ("shopping", _rx(r"шопинг|shopping|магазин|\bмолл|\bmall\b|сауда")),
    ("sport", _rx(r"спорт|sport")),
)

_CATEGORY_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("cafe", _rx(r"\bкафе|\bcafe|café|кофе|кофейн|coffee|кондитерск|пекарн|bakery|десерт"
                 r"|dessert|чайхан")),
    ("restaurant", _rx(r"ресторан|restaurant|поужинать|пообедать|\bужин|\bобед|dinner|lunch"
                       r"|мейрамхана")),
    ("fast_food", _rx(r"фаст-?фуд|fast food|бургер|burger|шаурм|донер|doner|пицц|pizza")),
    ("bar", _rx(r"\bбар(?:ы|е|ом|ах)?\b|\bпаб\w*|\bpubs?\b|\bbars?\b|коктейл|cocktail|\bпив\w*"
                r"|\bbeer")),
    ("nightlife", _rx(r"\bклуб\w*|дискотек|караоке|karaoke|nightclub|\bclubs?\b|тусовк"
                      r"|вечеринк|\bparty")),
    ("museum", _rx(r"музе|museum|мұражай|экспозиц")),
    ("gallery", _rx(r"галере|gallery|выставк|exhibition|көрме")),
    ("theatre", _rx(r"театр|theat|\bопер[аеуы]\b|балет|ballet|\bopera|спектакл")),
    ("cinema", _rx(r"\bкино|cinema|movie|фильм|\bfilm")),
    ("library", _rx(r"библиотек|library|кітапхана|читальн")),
    ("park", _rx(r"\bпарк(?!ов)\w*|сквер|бульвар|аллея|саябақ|\bparks?\b|garden|ботаническ")),
    ("playground", _rx(r"(?<!смотровая )(?<!смотровую )площадк|playground|ойын алаң|батут")),
    ("viewpoint", _rx(r"смотров\w* площадк|viewpoint|observation deck|lookout")),
    ("attraction", _rx(r"достопримечат|\bsights?\b|sightseeing|attraction|көрікті"
                       r"|что (?:можно )?посмотреть|what to see")),
    ("monument", _rx(r"памятник|монумент|monument|memorial|мемориал|ескерткіш|статуя|statue")),
    ("religious", _rx(r"мечет|церк|храм|собор|синагог|mosque|church|cathedral|мешіт")),
    ("mall", _rx(r"\bтц\b|\bтрц\b|торгов\w* центр|\bмолл|\bmall\b|сауда орталығ")),
    ("sport", _rx(r"спортзал|каток|бассейн|фитнес|скалодром|\bgym\b|swimming|ice rink|skating")),
    ("entertainment", _rx(r"развлеч|аттракцион|квест|боулинг|бильярд|аквапарк|entertainment"
                          r"|bowling|escape room|arcade|ойын-сауық")),
)
_FOOD_RE = _rx(r"поесть|перекусить|покушать|где поесть|\beat\b|тамақтан")

_GROUP_NUM_RES = (
    re.compile(r"\bнас\s+(\d+)"),
    re.compile(r"(\d+)\s*(?:человек\w*|чел\b|people|persons|adults|адам)"),
)
# Collective numerals: «нас пятеро», «вчетвером», «бесеуміз», "five of us", "a group of six".
_COLLECTIVE: dict[str, int] = {
    "двое": 2, "трое": 3, "четверо": 4, "пятеро": 5, "шестеро": 6, "семеро": 7,
    "восьмеро": 8, "девятеро": 9, "десятеро": 10,
    "вдвоем": 2, "втроем": 3, "вчетвером": 4, "впятером": 5, "вшестером": 6, "всемером": 7,
    "екеу": 2, "үшеу": 3, "төртеу": 4, "бесеу": 5, "алтау": 6, "жетеу": 7,
    "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
    "nine": 9, "ten": 10,
}
_COLLECTIVE_RES = (
    re.compile(r"\b(двое|трое|четверо|пятеро|шестеро|семеро|восьмеро|девятеро|десятеро"
               r"|вдвоем|втроем|вчетвером|впятером|вшестером|всемером)\b"),
    re.compile(r"\b(екеу|үшеу|төртеу|бесеу|алтау|жетеу)\w*(?:міз|мыз)"),
    re.compile(r"\b(?:the )?(two|three|four|five|six|seven|eight|nine|ten) of us\b"),
    re.compile(r"\b(?:group|party) of (two|three|four|five|six|seven|eight|nine|ten)\b"),
)
_GROUP_WORDS: tuple[tuple[re.Pattern[str], int], ...] = (
    (re.compile(r"\bдвоем|\bвдвоем"), 2),
    (re.compile(r"компани|с друзьями|друзь|friends|\bgroup\b|группой|достар"), GROUP_DEFAULT),
)


# --------------------------------------------------------------------- public


def detect_lang(text: str) -> Lang:
    """kk if Kazakh-specific letters (әғқңөұүһі) present, en if mostly Latin, else ru."""
    low = text.lower()
    if any(ch in _KK_LETTERS for ch in low):
        return "kk"
    words = re.findall(r"[^\W\d_]+", low)
    if sum(w in _KK_WORDS for w in words) >= _KK_WORDS_NEEDED:
        return "kk"
    latin = sum("a" <= ch <= "z" for ch in low)
    cyrillic = sum("Ѐ" <= ch <= "ӿ" for ch in low)
    return "en" if latin > cyrillic else "ru"


def parse_rules(text: str, lang_hint: Lang | None, landmarks: dict[str, Landmark]) -> QueryIntent:
    """Keywords -> categories / must_tags / with_kids / wheelchair / night / tourist /
    group_size; time: "30–60 минут", "1–2 часа", "полчаса", "пару часов", "2 hours", "2 сағат"."""
    t = _normalise(text)
    lang = lang_hint or detect_lang(text)

    max_walk, t_rest = _walk_minutes(t)
    budget = _time_budget(t_rest)
    if max_walk is None and budget is None and _NEAR_RE.search(t):
        max_walk = NEAR_WALK_MIN

    categories = [cat for cat, rx in _CATEGORY_PATTERNS if rx.search(t)]
    if lang == "kk" and "bar" in categories and not re.search(r"\bbars?\b|\bpubs?\b|паб", t):
        categories.remove("bar")  # Kazakh "бар" means "there is"
    if "viewpoint" in categories and "playground" in categories and "смотров" in t:
        categories.remove("playground")
    if "attraction" in categories:
        categories = _dedupe(categories + SIGHTS)
    if _FOOD_RE.search(t):
        categories = _dedupe(categories + FOOD)

    tags = [tag for tag, rx in _TAG_PATTERNS if rx.search(t)]
    indoor = "indoor" if _INDOOR_RE.search(t) else "outdoor" if _OUTDOOR_RE.search(t) else "any"

    return QueryIntent(
        raw_query=text,
        lang=lang,
        time_budget_min=budget,
        max_walk_min=max_walk,
        categories=categories,
        must_tags=[tag for tag in tags if tag in TAG_VOCAB],
        with_kids=bool(_KIDS_RE.search(t)),
        wheelchair=bool(_WHEEL_RE.search(t)),
        group_size=_group_size(t),
        indoor=indoor,
        night=bool(_NIGHT_RE.search(t)),
        tourist=bool(_TOURIST_RE.search(t)),
        landmark_mention=_landmark_id(text, landmarks),
        parser="rules",
    )


def parse_query(
    text: str,
    lang_hint: Lang | None,
    llm: LLMClient | None,
    landmarks: dict[str, Landmark],
) -> QueryIntent:
    """LLM first (if available), rules as fallback and gap filler. Sets intent.parser."""
    rules = parse_rules(text, lang_hint, landmarks)
    if llm is None or not llm.available:
        return rules
    try:
        llm_intent = llm.generate_structured(
            system=_system_prompt(landmarks),
            prompt=_user_prompt(text, lang_hint),
            schema=QueryIntent,
            effort="low",
        )
    except Exception as exc:  # noqa: BLE001 - LLMUnavailable or anything else: rules answer
        log.info("query parser: LLM unavailable (%s), using rules", type(exc).__name__)
        return rules
    return _merge(rules, llm_intent, lang_hint, landmarks)


def mentions_flight(text: str) -> bool:
    """A flight / the airport is mentioned (-> the general "leave time for the airport" tip)."""
    return bool(_FLIGHT_RE.search(_normalise(text)))


def departure_phrase(text: str) -> bool:
    """The time is said relative to the departure: «2 часа перед вылетом», «до вылета 2 часа»,
    «рейс через 3 часа», "before my flight", "flight in 2 hours", «ұшуға 2 сағат қалды».
    Only then the time budget is "time to departure" and the airport deadline rule applies;
    «прогулка на 30 минут, вечером рейс» keeps 30 minutes as the walk."""
    return bool(_DEPARTURE_RE.search(_normalise(text)))


def _departure_match(t: str) -> re.Match[str] | None:
    duration = (rf"(?:{_NUM}\s*(?:{_U_MIN}|{_U_HOUR})"
                r"|полтора часа|полчаса|час|an hour|one hour|two hours|three hours"
                r"|бір сағат|екі сағат|үш сағат)")
    flight = r"(?:вылет|рейс|самолет|посадк)\w*"
    patterns = (
        rf"{flight}\s+через\s+(?P<duration>{duration})",
        rf"(?:до|перед)\s+{flight}\s*(?:осталось\s+)?(?P<duration>{duration})",
        rf"(?P<duration>{duration})\s+(?:до|перед)\s+{flight}",
        rf"(?:flight|plane|departure)\s+(?:is\s+|leaves\s+)?in\s+(?P<duration>{duration})",
        rf"(?P<duration>{duration})\s+(?:before|until)\s+(?:my\s+|the\s+|our\s+)?"
        r"(?:flight|departure|plane)",
        rf"(?:ұшуға|ұшаққа|рейске)\s+(?:дейін\s+)?(?P<duration>{duration})",
    )
    for pattern in patterns:
        match = re.search(pattern, t)
        if match:
            return match
    return None


def departure_minutes(text: str) -> int | None:
    """Read the flight duration without imposing the activity budget's 600-minute cap."""
    match = _departure_match(_normalise(text))
    if match is None:
        return None
    duration = match.group("duration")
    numeric = _SINGLE_RE.fullmatch(duration)
    return (_to_minutes(numeric.group(1), numeric.group(2)) if numeric
            else _time_budget(duration))


def activity_minutes_before_flight(text: str) -> int | None:
    """Independent activity duration, regardless of whether the flight is mentioned first."""
    t = _normalise(text)
    match = _departure_match(t)
    if match is None:
        return None
    remaining = t[:match.start()] + " " + t[match.end():]
    _, remaining = _walk_minutes(remaining)
    return _time_budget(remaining)


def apply_overrides(intent: QueryIntent, overrides: UserOverrides | None) -> QueryIntent:
    """Explicit UI filters always win over the parsed intent.

    Mode switches (wave 3): `tourist`, `night` and `departure_in_min` (minutes to departure:
    tourist mode with that time budget). Read with getattr so this works whether or not the
    integrator's schema change has landed yet.
    """
    if overrides is None:
        return intent
    update: dict[str, object] = {}
    for name in ("time_budget_min", "max_walk_min", "with_kids", "wheelchair", "group_size",
                 "tourist", "night"):
        value = getattr(overrides, name, None)
        if value is not None:
            update[name] = value
    departure = getattr(overrides, "departure_in_min", None)
    if departure is not None:
        update["tourist"] = True
        update["time_budget_min"] = _clamp(int(departure), 5, 600)
    if overrides.categories is not None:
        update["categories"] = _dedupe(list(overrides.categories))
    if overrides.indoor_only is True:
        update["indoor"] = "indoor"
    elif overrides.indoor_only is False and intent.indoor == "indoor":
        update["indoor"] = "any"
    if not update:
        return intent
    return QueryIntent.model_validate({**intent.model_dump(), **update})


# -------------------------------------------------------------------- helpers


def _normalise(text: str) -> str:
    t = text.lower().replace("ё", "е").replace("–", "-").replace("—", "-")
    return re.sub(r"\s+", " ", t)


def _to_minutes(value: str, unit: str) -> int:
    number = float(value.replace(",", "."))
    is_minutes = unit.startswith(("мин", "min"))
    return round(number if is_minutes else number * 60)


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, value))


def _walk_minutes(t: str) -> tuple[int | None, str]:
    """Explicit walking limit and the text without it (so it is not read as a budget)."""
    for rx in _WALK_RES:
        m = rx.search(t)
        if m:
            minutes = _clamp(round(float(m.group(1).replace(",", "."))), 1, 120)
            return minutes, t[: m.start()] + " " + t[m.end() :]
    return None, t


def _time_budget(t: str) -> int | None:
    m = _RANGE_RE.search(t)
    if m:
        return _clamp(_to_minutes(m.group(2), m.group(3)), 5, 600)
    for rx, minutes in _WORD_BUDGETS[:-1]:  # specific phrases before bare "час"
        if rx.search(t):
            return minutes
    m = _SINGLE_RE.search(t)
    if m:
        return _clamp(_to_minutes(m.group(1), m.group(2)), 5, 600)
    rx, minutes = _WORD_BUDGETS[-1]
    return minutes if rx.search(t) else None


def _group_size(t: str) -> int | None:
    for rx in _GROUP_NUM_RES:
        m = rx.search(t)
        if m:
            return _clamp(int(m.group(1)), 1, 20)
    for rx in _COLLECTIVE_RES:
        m = rx.search(t)
        if m:
            return _COLLECTIVE[m.group(1)]
    for rx, size in _GROUP_WORDS:
        if rx.search(t):
            return size
    return None


def _landmark_id(text: str, landmarks: dict[str, Landmark]) -> str | None:
    if not landmarks:
        return None
    try:
        lm = geo.match_landmark(text, landmarks)
    except Exception as exc:  # noqa: BLE001 - a landmark hint is optional
        log.warning("match_landmark failed: %s", type(exc).__name__)
        return None
    return lm.id if lm is not None and lm.id in landmarks else None


def _dedupe(items: list[str]) -> list[str]:
    return list(dict.fromkeys(items))


def _system_prompt(landmarks: dict[str, Landmark]) -> str:
    lm_lines = "\n".join(
        f"- {lm.id}: " + " / ".join(dict.fromkeys(lm.names.values()))
        for lm in sorted(landmarks.values(), key=lambda x: x.id)
    ) or "- (нет)"
    return (
        load_prompt("parse_query")
        .replace("{{CATEGORIES}}", ", ".join(CATEGORIES))
        .replace("{{TAGS}}", ", ".join(sorted(TAG_VOCAB)))
        .replace("{{LANDMARKS}}", lm_lines)
    )


def _user_prompt(text: str, lang_hint: Lang | None) -> str:
    hint = f"Язык интерфейса: {lang_hint}.\n" if lang_hint else ""
    return f"{hint}Разбери запрос пользователя.\n{user_data_block(text)}"


def _merge(
    rules: QueryIntent,
    llm: QueryIntent,
    lang_hint: Lang | None,
    landmarks: dict[str, Landmark],
) -> QueryIntent:
    if lang_hint:
        lang: Lang = lang_hint
    elif rules.lang != "ru":
        lang = rules.lang  # Kazakh letters / Latin script are strong signals
    else:
        lang = llm.lang

    llm_cats = [c for c in llm.categories if c in CATEGORIES]
    avoid = _dedupe([t for t in llm.avoid_tags if t in TAG_VOCAB] + rules.avoid_tags)
    must = _dedupe([t for t in llm.must_tags if t in TAG_VOCAB] + rules.must_tags)
    landmark = (llm.landmark_mention if llm.landmark_mention in landmarks
                else rules.landmark_mention)

    budget = llm.time_budget_min or rules.time_budget_min
    if budget:
        # With a time budget the radius comes from it: only an explicit walking limit counts
        # (the LLM tends to turn "поблизости" into 15 min even when a budget is given).
        max_walk = _walk_minutes(_normalise(rules.raw_query))[0]
    else:
        max_walk = llm.max_walk_min or rules.max_walk_min

    return QueryIntent(
        raw_query=rules.raw_query,
        lang=lang,
        time_budget_min=budget,
        max_walk_min=max_walk,
        categories=_dedupe(llm_cats) or rules.categories,
        must_tags=[t for t in must if t not in avoid],
        avoid_tags=avoid,
        with_kids=llm.with_kids or rules.with_kids,
        wheelchair=llm.wheelchair or rules.wheelchair,
        group_size=llm.group_size or rules.group_size,
        indoor=llm.indoor if llm.indoor != "any" else rules.indoor,
        night=llm.night or rules.night,
        tourist=llm.tourist or rules.tourist,
        landmark_mention=landmark,
        parser="llm",
    )
