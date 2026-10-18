"""Retrieval: hard filters -> scoring (semantic or lexical) -> rerank + diversity.

Owner: CORE lane (task T22). Deterministic, no network. See docs/ARCHITECTURE.md §RAG.

Weights (tuned by eval in Wave 3, see eval/TUNING.md; keep them in WEIGHTS):
  score = 0.55*relevance + 0.20*proximity + 0.10*open_score + 0.05*popularity
        + 0.05*data_confidence + 0.05*weather_fit
relevance = cosine(query_vec, poi_vec) if embeddings else lexical overlap.
With embeddings the cosine is blended with lexical overlap (SEMANTIC_SHARE) because exact
tags such as power_outlets get blurred in embedding space.
Final order = MMR (MMR_LAMBDA) with a top-3 diversity rule; Candidate.score stays the raw score.
"""

from __future__ import annotations

import logging
import re
from functools import lru_cache

import numpy as np

from app.geo import haversine_m, walk_minutes
from app.hours import evaluate_hours
from app.schemas import POI, Candidate, HoursVerdict, Lang, QueryIntent, SearchContext
from app.store import Store

log = logging.getLogger(__name__)

# T70 step 1 (eval/TUNING.md): relevance 0.45 -> 0.55; open 0.15 -> 0.10 (72% of POI have no
# hours, parks never do); popularity 0.10 -> 0.05 (a proxy for record completeness, not quality)
WEIGHTS: dict[str, float] = {
    "relevance": 0.55,
    "proximity": 0.20,
    "open": 0.10,
    "popularity": 0.05,
    "confidence": 0.05,
    "weather": 0.05,
}
MMR_LAMBDA = 0.7
# score_parts that explain the score but are not added to it
#   semantic  — the cosine actually used for this POI (absent = lexical only / vector dropped)
#   no_vector — this POI has no vector while others do: its semantic part is the pool median
#   mmr       — the MMR value that decided the order
INFO_PARTS = frozenset({"mmr", "semantic", "no_vector"})

SEMANTIC_SHARE = 0.7  # relevance = 0.7*cosine + 0.3*lexical when embeddings are available
MIN_VISIT_MIN = 30  # drop places that close sooner than this
MMR_POOL = 24
TOP_N = 3  # diversity rules apply to the first TOP_N results
MAX_SAME_CATEGORY = 2
DIVERSITY_GUARD = 0.75  # swap for diversity only if the alternative keeps >= 75% of the score
WHEELCHAIR_UNKNOWN_PENALTY = -0.10
MAX_REASONS = 4

OPEN_SCORE: dict[tuple[str, str], float] = {("open", "high"): 1.0, ("open", "medium"): 0.8}
UNKNOWN_OPEN_SCORE = 0.5
# T70 step 4: at night a place that closes within the hour is a poor first pick (S5)
NIGHT_MIN_OPEN_MIN = 60
NIGHT_CLOSING_SOON_SCORE = 0.3
# T70 step 5: a group of 3+ wants room and something to do together (no capacity data in OSM,
# so it is a category / tag heuristic; it only re-orders, it never filters)
GROUP_MIN = 3
GROUP_BONUS = 0.05
GROUP_FRIENDLY = frozenset(
    {"restaurant", "bar", "nightlife", "mall", "cinema", "entertainment", "sport", "theatre"}
    | {"park", "attraction"}
)
GROUP_UNFRIENDLY_TAGS = frozenset({"quiet", "work_friendly"})
# T70 step 15: the user named tags (coffee, power_outlets, quiet …) and the place has none of
# them -> it must not ride on proximity / popularity / MMR diversity past places that match
NO_TAG_MATCH_PENALTY = -0.10

KIDS_CATEGORIES = frozenset({"playground", "park", "museum", "attraction", "mall"})
ADULT_CATEGORIES = frozenset({"bar", "nightlife"})
INDOOR_CATEGORIES = frozenset(
    {"cafe", "restaurant", "fast_food", "bar", "nightlife", "museum", "gallery", "theatre"}
    | {"cinema", "library", "mall"}
)
OUTDOOR_CATEGORIES = frozenset({"park", "playground", "viewpoint", "monument"})
# E2 (T70): the OSM-derived tag `night` says "a night venue", not "open now"; at night only
# confirmed hours (open/high) or late_open / 24_7 let a place through
NIGHT_TAGS = frozenset({"late_open", "24_7"})

# Words that make POI text findable by ru / kk / en queries without embeddings.
CATEGORY_WORDS: dict[str, str] = {
    "cafe": "кафе кофейня кофе cafe coffee дәмхана кофехана",
    "restaurant": "ресторан поесть ужин обед restaurant dinner lunch мейрамхана тамақ",
    "fast_food": "фастфуд перекус перекусить бургер fast food snack тамақ",
    "bar": "бар паб коктейль bar pub cocktail",
    "nightlife": "клуб ночной тусовка nightlife club түнгі клуб",
    "museum": "музей экспозиция выставка museum exhibition мұражай көрме",
    "gallery": "галерея выставка искусство gallery art exhibition көрме өнер",
    "theatre": "театр спектакль theatre theater play театр",
    "cinema": "кино кинотеатр фильм cinema movie film",
    "library": "библиотека книги читать library books кітапхана",
    "park": "парк сквер аллея прогулка погулять гулять park garden walk саябақ серуен",
    "playground": "детская площадка игровая дети playground kids балалар алаңы ойын",
    "viewpoint": "смотровая площадка вид панорама viewpoint view panorama көрініс",
    "attraction": "достопримечательность аттракцион интересное attraction sight көрікті",
    "monument": "памятник монумент мемориал monument memorial ескерткіш",
    "religious": "мечеть храм церковь собор mosque church мешіт",
    "mall": "трц торговый центр магазины mall shopping сауда орталығы",
    "sport": "спорт фитнес бассейн стадион каток sport gym pool",
    "entertainment": "развлечения боулинг квест игры entertainment fun ойын",
    "other": "",
}
TAG_WORDS: dict[str, str] = {
    "coffee": "кофе кофейня coffee",
    "wifi": "wifi вайфай интернет",
    "power_outlets": "розетка розетки зарядить зарядка ноутбук outlet socket charge",
    "quiet": "тихо тихое тихий тихая спокойно спокойное уединение quiet calm тыныш",
    "work_friendly": "работать поработать ноутбук коворкинг work laptop жұмыс",
    "kids": "дети детский детская ребенок ребенка малыш семья kids children family бала балалар",
    "interactive": "интерактив интерактивный интерактивная интерактивы interactive",
    "indoor": "внутри помещение крытый тепло indoor",
    "outdoor": "улица воздух открытый outdoor",
    "view": "вид панорама смотровая view panorama көрініс",
    "photo_spot": "фото фотографии красиво инстаграм photo",
    "night": "ночь ночью ночной поздно night түн түнгі",
    "late_open": "поздно допоздна вечер вечером ночью late кеш",
    "24_7": "круглосуточно ночь",
    "free": "бесплатно бесплатный free тегін",
    "wheelchair": "коляска инвалид пандус доступная wheelchair accessible",
    "vegetarian": "вегетарианский веган vegetarian vegan",
    "local_cuisine": "казахская национальная кухня бешбармак kazakh local ұлттық",
    "live_music": "живая музыка концерт live music",
    "walk": "прогулка погулять гулять пройтись walk серуен",
    "water": "вода набережная река озеро фонтан water river өзен",
    "architecture": "архитектура здание architecture сәулет",
    "history": "история исторический историческое history тарих",
    "art": "искусство арт картины art өнер",
    "shopping": "шопинг магазины покупки shopping дүкен",
    "sport": "спорт тренировка sport",
    "food": "еда поесть перекусить food тамақ",
}
STOPWORDS = frozenset(
    """
    я мы вы ты он она они меня мне нас нам вас где что как куда хочу хотим хочется хотел хотела
    можно нужно надо есть рядом недалеко поблизости около возле сейчас тут здесь там место места
    посоветуйте посоветуете подскажите порекомендуйте какой какое какая какие и в на с со у к по
    за из до для о об от или но а это мой моя мое мои минут минуты минута час часа часов пару
    пара очень бы ли не нет да еще уже тоже сходить пойти куда-нибудь что-нибудь что-то
    the a an and or to of in on at for with near me i we want some something place where what
    is are can any go
    мен біз қайда керек жақын жерде маған
    """.split()
)
GENERIC_NAMES = frozenset(
    {
        "детская площадка",
        "детская игровая площадка",
        "игровая площадка",
        "площадка",
        "playground",
        "балалар алаңы",
        "балалар ойын алаңы",
        "спортивная площадка",
        "сквер",
        "парк",
        "кафе",
        "кофейня",
        "ресторан",
        "памятник",
        "смотровая площадка",
        "фонтан",
    }
)

_WORD = re.compile(r"\w+")
_STEM = 5
_MIN_PREFIX = 4

SOURCE_LABELS: dict[str, str] = {
    "osm": "OSM",
    "wikidata": "Wikidata",
    "wikipedia": "Wikipedia",
    "google_places": "Google",
    "manual": "данные команды",
    "mock": "мок",
}
FACT_TAGS = frozenset({"coffee", "wifi", "power_outlets"})  # reasons that cite the source
# T70 step 7: requested amenities the record says nothing about are named honestly
MISSING_TAG_WORDS: dict[str, dict[str, str]] = {
    "coffee": {"ru": "кофе", "kk": "кофе", "en": "coffee"},
    "power_outlets": {"ru": "розетки", "kk": "розетка", "en": "power outlets"},
    "wifi": {"ru": "Wi-Fi", "kk": "Wi-Fi", "en": "Wi-Fi"},
    "quiet": {"ru": "тишина", "kk": "тыныштық", "en": "noise level"},
    "work_friendly": {"ru": "место для работы", "kk": "жұмыс орны", "en": "space to work"},
    "vegetarian": {"ru": "вегетарианское меню", "kk": "вегетариандық ас", "en": "vegetarian food"},
    "local_cuisine": {"ru": "местная кухня", "kk": "ұлттық ас", "en": "local cuisine"},
}
REASONS: dict[str, dict[str, str]] = {
    "walk": {"ru": "≈{n} мин пешком", "kk": "≈{n} мин жаяу", "en": "≈{n} min walk"},
    "coffee": {"ru": "есть кофе ({src})", "kk": "кофе бар ({src})", "en": "coffee ({src})"},
    "wifi": {"ru": "есть Wi-Fi ({src})", "kk": "Wi-Fi бар ({src})", "en": "Wi-Fi ({src})"},
    "power_outlets": {
        "ru": "есть розетки ({src})",
        "kk": "розетка бар ({src})",
        "en": "power outlets ({src})",
    },
    "quiet": {"ru": "тихое место", "kk": "тыныш орын", "en": "quiet place"},
    "work_friendly": {
        "ru": "удобно поработать",
        "kk": "жұмыс істеуге ыңғайлы",
        "en": "good for working",
    },
    "kids": {"ru": "подходит детям", "kk": "балаларға қолайлы", "en": "good for kids"},
    "interactive": {
        "ru": "интерактивная экспозиция",
        "kk": "интерактивті экспозиция",
        "en": "interactive exhibits",
    },
    "view": {"ru": "вид на город", "kk": "қалаға көрініс", "en": "city view"},
    "photo_spot": {"ru": "место для фото", "kk": "суретке түсетін орын", "en": "photo spot"},
    "night": {"ru": "подходит для вечера", "kk": "кешке қолайлы", "en": "good for the evening"},
    "late_open": {"ru": "работает допоздна", "kk": "кеш жабылады", "en": "open late"},
    "24_7": {"ru": "круглосуточно", "kk": "тәулік бойы", "en": "open 24/7"},
    "free": {"ru": "вход бесплатный", "kk": "кіру тегін", "en": "free entry"},
    "wheelchair": {
        "ru": "доступно для коляски",
        "kk": "арбамен кіруге болады",
        "en": "wheelchair accessible",
    },
    "wheelchair_limited": {
        "ru": "частично доступно для коляски",
        "kk": "арбамен ішінара кіруге болады",
        "en": "partly wheelchair accessible",
    },
    "wheelchair_unknown": {
        "ru": "доступность не подтверждена",
        "kk": "қолжетімділігі расталмаған",
        "en": "accessibility not confirmed",
    },
    "vegetarian": {"ru": "есть вегетарианское", "kk": "вегетариандық ас бар", "en": "vegetarian"},
    "local_cuisine": {"ru": "казахская кухня", "kk": "қазақ асханасы", "en": "Kazakh cuisine"},
    "live_music": {"ru": "живая музыка", "kk": "тірі музыка", "en": "live music"},
    "walk_tag": {"ru": "можно прогуляться", "kk": "серуендеуге болады", "en": "nice for a walk"},
    "water": {"ru": "у воды", "kk": "су жағасында", "en": "by the water"},
    "architecture": {
        "ru": "интересная архитектура",
        "kk": "қызық сәулет",
        "en": "notable architecture",
    },
    "history": {"ru": "про историю", "kk": "тарихи орын", "en": "history spot"},
    "art": {"ru": "искусство", "kk": "өнер", "en": "art to see"},
    "shopping": {"ru": "магазины", "kk": "дүкендер", "en": "shops inside"},
    "food": {"ru": "можно поесть", "kk": "тамақтануға болады", "en": "serves food"},
    "indoor_weather": {
        "ru": "в помещении — укрытие от погоды",
        "kk": "ғимарат ішінде — ауа райынан пана",
        "en": "indoors, sheltered from the weather",
    },
    "outdoor_weather": {
        "ru": "погода подходит для улицы",
        "kk": "ауа райы серуенге қолайлы",
        "en": "good weather for outdoors",
    },
    "closes_soon": {
        "ru": "до закрытия ≈{n} мин",
        "kk": "жабылуына ≈{n} мин",
        "en": "closes in ≈{n} min",
    },
    "no_data": {"ru": "нет данных: {what}", "kk": "дерек жоқ: {what}", "en": "no data: {what}"},
    "hours_unknown": {
        "ru": "часы работы не указаны",
        "kk": "жұмыс уақыты көрсетілмеген",
        "en": "opening hours not listed",
    },
}
_TAG_REASON_KEY = {"walk": "walk_tag"}


# ------------------------------------------------------------------ helpers


def _normalize(text: str) -> str:
    return text.lower().replace("ё", "е")


def _tokens(text: str) -> list[str]:
    return [
        t
        for t in _WORD.findall(_normalize(text))
        if len(t) > 1 and not t.isdigit() and t not in STOPWORDS
    ]


@lru_cache(maxsize=16384)
def _doc_index(text: str) -> tuple[frozenset[str], frozenset[str], frozenset[str]]:
    """(tokens, 5-char stems, 4-char prefixes of stems) of a POI text; cached per text."""
    tokens = _tokens(text)
    stems = frozenset(t[:_STEM] for t in tokens)
    prefixes = frozenset(d[:_MIN_PREFIX] for d in stems if len(d) >= _MIN_PREFIX)
    return frozenset(tokens), stems, prefixes


def text_stems(text: str) -> frozenset[str]:
    """5-char stems of meaningful words (stopwords dropped); shared with app.validate."""
    return frozenset(t[:_STEM] for t in _tokens(text))


def _query_stems(intent: QueryIntent) -> frozenset[str]:
    return frozenset(t[:_STEM] for t in _tokens(intent.raw_query))


def _landmark_stems(store: Store, intent: QueryIntent, ctx: SearchContext) -> frozenset[str]:
    """Alias words of the landmark the user stands at: «я возле Mega Silk Way» says where the
    user is, not what they look for, so these words must not make the mall a match."""
    words: list[str] = []
    for lm_id in {ctx.landmark_id, intent.landmark_mention} - {None}:
        lm = store.landmarks.get(lm_id) if lm_id else None
        if lm is not None:
            words.extend(lm.aliases)
    return frozenset(t[:_STEM] for t in _tokens(" ".join(words)))


def _indexed_hit(stem: str, stems: frozenset[str], prefixes: frozenset[str]) -> bool:
    """Same rule as _stem_hit in O(1): stems are at most _STEM chars long."""
    if stem in stems:
        return True
    if len(stem) < _MIN_PREFIX:
        return False
    return stem in prefixes or stem[:_MIN_PREFIX] in stems


def _stem_hit(stem: str, doc_stems: set[str] | frozenset[str]) -> bool:
    if stem in doc_stems:
        return True
    if len(stem) < _MIN_PREFIX:
        return False
    return any(
        len(d) >= _MIN_PREFIX and (d.startswith(stem) or stem.startswith(d)) for d in doc_stems
    )


def poi_text(poi: POI) -> str:
    """Searchable text of a POI: names, description, category and tag words (ru/kk/en)."""
    parts = [poi.name, *poi.name_i18n.values(), poi.description or "", poi.category]
    parts.append(CATEGORY_WORDS.get(poi.category, ""))
    for tag in poi.tags:
        parts.extend([tag, TAG_WORDS.get(tag, "")])
    return " ".join(parts)


def _is_indoor(poi: POI) -> bool | None:
    if poi.indoor is not None:
        return poi.indoor
    if "indoor" in poi.tags:
        return True
    if "outdoor" in poi.tags:
        return False
    if poi.category in INDOOR_CATEGORIES:
        return True
    if poi.category in OUTDOOR_CATEGORIES:
        return False
    return None


def _access(poi: POI) -> str:
    if poi.wheelchair == "no":
        return "no"
    if poi.wheelchair == "yes" or (poi.wheelchair == "unknown" and "wheelchair" in poi.tags):
        return "yes"
    return "limited" if poi.wheelchair == "limited" else "unknown"


def _kids_ok(poi: POI) -> bool:
    if poi.kids_friendly is False or poi.category in ADULT_CATEGORIES:
        return False
    return bool(poi.kids_friendly) or "kids" in poi.tags or poi.category in KIDS_CATEGORIES


def _bad_weather(ctx: SearchContext) -> bool | None:
    return None if ctx.weather is None else not ctx.weather.outdoor_ok


def generic_key(poi: POI) -> str | None:
    """Same key for unnamed places like the 23 «Детская площадка» in the OSM fixture."""
    name = _normalize(poi.name).strip(" «»\"'")
    return f"{poi.category}:{name}" if name in GENERIC_NAMES or name == poi.category else None


# ------------------------------------------------------------------ filters


def _passes_profile(poi: POI, intent: QueryIntent) -> bool:
    if intent.categories and poi.category not in intent.categories:
        return False
    if intent.avoid_tags and set(poi.tags) & set(intent.avoid_tags):
        return False
    if intent.with_kids and not _kids_ok(poi):
        return False
    if intent.wheelchair and _access(poi) == "no":
        return False
    indoor = _is_indoor(poi)
    if intent.indoor == "indoor" and indoor is False:
        return False
    return not (intent.indoor == "outdoor" and indoor is True)


def _hours_drop_reason(poi: POI, verdict: HoursVerdict, intent: QueryIntent) -> str | None:
    """Why the hours filter drops a place (None = it passes). Keys of hours_dropped()."""
    if verdict.status == "closed" and verdict.confidence == "high":
        return "closed"
    if verdict.status == "closed" and verdict.confidence == "medium":
        return "closed_partial"  # today not described in OSM: closed by the OSM spec
    if verdict.status == "open" and verdict.closes_in_min is not None:
        if verdict.closes_in_min < MIN_VISIT_MIN:
            return "closing_soon"
    if intent.night:
        open_now = verdict.status == "open" and verdict.confidence == "high"
        if not (open_now or NIGHT_TAGS & set(poi.tags)):
            return "night"
    return None


def _passes_hours(poi: POI, verdict: HoursVerdict, intent: QueryIntent) -> bool:
    return _hours_drop_reason(poi, verdict, intent) is None


def hours_dropped(store: Store, intent: QueryIntent, ctx: SearchContext) -> dict[str, int]:
    """How many places in the radius (after profile filters) the hours filter removed, by
    reason: closed, closed_partial (today not described in OSM), closing_soon, night.
    For the pipeline's filters panel (FEATURES); retrieve() itself stays unchanged."""
    counts = {"closed": 0, "closed_partial": 0, "closing_soon": 0, "night": 0}
    for poi in store.pois:
        if haversine_m(ctx.origin, poi.geo) > ctx.radius_m or not _passes_profile(poi, intent):
            continue
        reason = _hours_drop_reason(poi, evaluate_hours(poi.opening_hours, ctx.local_time),
                                    intent)
        if reason is not None:
            counts[reason] += 1
    return counts


# ------------------------------------------------------------------ scoring


def _lexical(intent: QueryIntent, query_stems: frozenset[str], text: str) -> float:
    doc_set, doc_stems, doc_prefixes = _doc_index(text)
    if not doc_set:
        return 0.0
    parts: list[tuple[float, float]] = []
    if query_stems:
        hits = sum(_indexed_hit(s, doc_stems, doc_prefixes) for s in query_stems)
        parts.append((0.5, hits / len(query_stems)))
    if intent.must_tags:
        parts.append((0.3, sum(t in doc_set for t in intent.must_tags) / len(intent.must_tags)))
    if intent.categories:
        parts.append((0.2, 1.0 if any(c in doc_set for c in intent.categories) else 0.0))
    if not parts:
        return 0.0
    return round(sum(w * v for w, v in parts) / sum(w for w, _ in parts), 4)


def lexical_relevance(intent: QueryIntent, text: str) -> float:
    """0..1 keyword overlap between intent (raw_query + tags + categories) and POI text."""
    return _lexical(intent, _query_stems(intent), text)


def _semantic_scores(store: Store, query_vec: np.ndarray | None) -> np.ndarray | None:
    """Cosine per POI in 0..1; NaN where the POI has no vector (it is scored lexically)."""
    if query_vec is None or store.embeddings is None:
        return None
    q = np.asarray(query_vec, dtype=np.float32).ravel()
    if store.embeddings.ndim != 2 or q.shape[0] != store.embeddings.shape[1]:
        log.warning("query vector dim %d does not match the index, lexical only", q.shape[0])
        return None
    norm = float(np.linalg.norm(q))
    if not np.isfinite(norm) or norm == 0.0:
        log.warning("query vector is zero or not finite, lexical only")
        return None
    scores = np.clip(store.embeddings @ (q / norm), 0.0, 1.0)
    has_vector = np.linalg.norm(store.embeddings, axis=1) > 0
    return np.where(has_vector & np.isfinite(scores), scores, np.nan)


def _weather_fit(poi: POI, ctx: SearchContext) -> float:
    bad = _bad_weather(ctx)
    indoor = _is_indoor(poi)
    if bad is None or indoor is None:
        return 0.7
    if bad:
        return 1.0 if indoor else 0.2
    return 0.7 if indoor else 1.0


def _group_fit(poi: POI, intent: QueryIntent) -> float:
    if poi.category in GROUP_FRIENDLY:
        return GROUP_BONUS
    unfriendly = poi.category == "library" or bool(GROUP_UNFRIENDLY_TAGS & set(poi.tags))
    if unfriendly or (poi.category == "playground" and not intent.with_kids):
        return -GROUP_BONUS
    return 0.0


def _open_score(verdict: HoursVerdict, intent: QueryIntent) -> float:
    if (
        intent.night
        and verdict.status == "open"
        and verdict.closes_in_min is not None
        and verdict.closes_in_min < NIGHT_MIN_OPEN_MIN
    ):
        return NIGHT_CLOSING_SOON_SCORE
    return OPEN_SCORE.get((verdict.status, verdict.confidence), UNKNOWN_OPEN_SCORE)


def _score(
    poi: POI,
    intent: QueryIntent,
    ctx: SearchContext,
    distance: float,
    verdict: HoursVerdict,
    semantic: float | None,
    query_stems: frozenset[str],
) -> tuple[float, dict[str, float]]:
    lexical = _lexical(intent, query_stems, poi_text(poi))
    relevance = lexical if semantic is None else (
        SEMANTIC_SHARE * semantic + (1 - SEMANTIC_SHARE) * lexical
    )
    parts = {
        "relevance": relevance,
        "proximity": max(0.0, 1.0 - distance / ctx.radius_m),
        "open": _open_score(verdict, intent),
        "popularity": poi.popularity,
        "confidence": poi.data_confidence,
        "weather": _weather_fit(poi, ctx),
    }
    score = sum(WEIGHTS[k] * v for k, v in parts.items())
    if intent.group_size is not None and intent.group_size >= GROUP_MIN:
        parts["group"] = _group_fit(poi, intent)
        score += parts["group"]
    if intent.must_tags and not set(intent.must_tags) & set(poi.tags):
        parts["no_tags"] = NO_TAG_MATCH_PENALTY
        score += NO_TAG_MATCH_PENALTY
    if intent.wheelchair and _access(poi) == "unknown":
        parts["penalty"] = WHEELCHAIR_UNKNOWN_PENALTY
        score += WHEELCHAIR_UNKNOWN_PENALTY
    return round(score, 4), {k: round(float(v), 4) for k, v in parts.items()}


# ---------------------------------------------------------------- diversity


def _similarity(a: POI, b: POI) -> float:
    key = generic_key(a)
    if key is not None and key == generic_key(b):
        return 1.0
    tags_a, tags_b = set(a.tags), set(b.tags)
    union = tags_a | tags_b
    jaccard = len(tags_a & tags_b) / len(union) if union else 0.0
    return 0.6 * (a.category == b.category) + 0.4 * jaccard


def _violation(cand: Candidate, selected: list[Candidate]) -> str | None:
    """Top-3 rules: one unnamed place per name, at most two places of one category."""
    if len(selected) >= TOP_N:
        return None
    key = generic_key(cand.poi)
    if key is not None and any(generic_key(s.poi) == key for s in selected):
        return "generic"
    if sum(s.poi.category == cand.poi.category for s in selected) >= MAX_SAME_CATEGORY:
        return "category"
    return None


def _mmr(pool: list[Candidate], k: int) -> list[Candidate]:
    selected: list[Candidate] = []
    remaining = list(pool)
    # scores live in ~0.3..0.7 while similarity is 0..1: min-max normalise over the pool so
    # that lambda really balances relevance against redundancy
    low = min((c.score for c in pool), default=0.0)
    spread = max((c.score for c in pool), default=0.0) - low
    relevance = {c.poi.id: (c.score - low) / spread if spread > 0 else 1.0 for c in pool}
    while remaining and len(selected) < k:
        ranked: list[tuple[float, Candidate]] = []
        for c in remaining:
            redundancy = max((_similarity(c.poi, s.poi) for s in selected), default=0.0)
            ranked.append(
                (MMR_LAMBDA * relevance[c.poi.id] - (1 - MMR_LAMBDA) * redundancy, c)
            )
        # best MMR value first; ties: higher raw score, nearer, id
        ranked.sort(key=lambda item: (-item[0], -item[1].score, item[1].distance_m, item[1].poi.id))
        value, pick = ranked[0]
        problem = _violation(pick, selected)
        if problem is not None:
            for alt_value, alt in ranked[1:]:
                if _violation(alt, selected) is not None:
                    continue
                if problem == "generic" or alt.score >= DIVERSITY_GUARD * pick.score:
                    value, pick = alt_value, alt
                break
        pick.score_parts["mmr"] = round(value, 4)
        selected.append(pick)
        remaining.remove(pick)
    return selected


# ------------------------------------------------------------------ reasons


def _source_label(poi: POI) -> str:
    for source in ("osm", "wikidata", "wikipedia", "google_places", "manual", "mock"):
        if source in poi.sources:
            return SOURCE_LABELS[source]
    return SOURCE_LABELS.get(poi.sources[0], poi.sources[0])


def _reason(key: str, lang: Lang, **fmt: object) -> str:
    texts = REASONS[key]
    return texts.get(lang, texts["ru"]).format(**fmt)


def explain(
    cand: Candidate, intent: QueryIntent, ctx: SearchContext, lang: Lang = "ru"
) -> list[str]:
    """Short deterministic reasons for a candidate (CORE-internal: retrieval, validate)."""
    poi = cand.poi
    src = _source_label(poi)
    keys: list[tuple[str, dict[str, object]]] = [("walk", {"n": cand.walk_min})]

    query_stems = _query_stems(intent)
    wanted = [t for t in intent.must_tags if t in poi.tags]
    for tag in poi.tags:  # tags the user asked for in words, e.g. "кофе" without must_tags
        words = {t[:_STEM] for t in _tokens(TAG_WORDS.get(tag, ""))}
        if tag not in wanted and any(_stem_hit(s, words) for s in query_stems):
            wanted.append(tag)
    for tag in wanted:
        key = _TAG_REASON_KEY.get(tag, tag)
        if key in REASONS and key not in ("indoor", "outdoor"):
            keys.append((key, {"src": src} if tag in FACT_TAGS else {}))

    missing = [t for t in intent.must_tags if t in MISSING_TAG_WORDS and t not in poi.tags]
    if missing:
        labels = (MISSING_TAG_WORDS[t] for t in missing)
        what = ", ".join(label.get(lang, label["ru"]) for label in labels)
        keys.append(("no_data", {"what": what}))
    if intent.with_kids and (poi.kids_friendly or "kids" in poi.tags):  # data, not a guess
        keys.append(("kids", {}))
    if intent.wheelchair:
        access = _access(poi)
        keys.append(({"yes": "wheelchair", "limited": "wheelchair_limited"}.get(
            access, "wheelchair_unknown"), {}))
    bad = _bad_weather(ctx)
    indoor = _is_indoor(poi)
    if bad is True and indoor is True:
        keys.append(("indoor_weather", {}))
    elif bad is False and indoor is False:
        keys.append(("outdoor_weather", {}))
    if intent.night:
        for tag in ("24_7", "late_open"):
            if tag in poi.tags:
                keys.append((tag, {}))
                break
    hours = cand.hours
    if hours.status == "unknown":
        keys.append(("hours_unknown", {}))
    elif hours.closes_in_min is not None and hours.closes_in_min <= 90:
        keys.append(("closes_soon", {"n": hours.closes_in_min}))

    reasons: list[str] = []
    seen: set[str] = set()
    for key, fmt in keys:
        if key in seen:
            continue
        seen.add(key)
        reasons.append(_reason(key, lang, **fmt))
    if len(reasons) > MAX_REASONS:  # keep the hours caveat: it is about honesty
        tail = reasons[-1] if keys[-1][0] in ("hours_unknown", "closes_soon") else None
        reasons = reasons[: MAX_REASONS - 1] + [tail] if tail else reasons[:MAX_REASONS]
    return reasons


# ------------------------------------------------------------ no-match signal


def query_unmatched(
    store: Store, intent: QueryIntent, ctx: SearchContext, candidates: list[Candidate]
) -> bool:
    """True when the free-text query asked for something and no candidate matched it.

    «порекомендуй казино», gibberish: the answer is then just the nearest places. The pipeline
    (FEATURES) adds a warning like «точного совпадения нет, показано, что рядом» (ru/kk/en).
    False for structured requests (must_tags / categories), queries made only of stopwords
    or landmark words («я возле Mega Silk Way»), and when nothing was found at all.
    """
    if not candidates or intent.must_tags or intent.categories:
        return False
    if not _query_stems(intent) - _landmark_stems(store, intent, ctx):
        return False
    return max(c.score_parts.get("relevance", 0.0) for c in candidates) == 0.0


# -------------------------------------------------------------------- main


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
    high confidence or closing before a minimal visit; night -> open/high or late_open / 24_7.
    Unknown hours are allowed but penalised. Fill Candidate.matched with short
    deterministic reasons in Russian (e.g. "есть кофе", "≈6 мин пешком").
    Final order is MMR (score vs. redundancy); Candidate.score keeps the raw weighted score.
    Unnamed places with the same generic name are kept once (the best one).
    """
    if k <= 0 or not store.pois:
        return []
    semantic = _semantic_scores(store, query_vec)
    query_stems = _query_stems(intent) - _landmark_stems(store, intent, ctx)
    passed: list[tuple[int, POI, float, HoursVerdict]] = []
    for i, poi in enumerate(store.pois):
        distance = haversine_m(ctx.origin, poi.geo)
        if distance > ctx.radius_m or not _passes_profile(poi, intent):
            continue
        # the note is written in the user's language by app.hours (FEATURES, T73)
        verdict = evaluate_hours(poi.opening_hours, ctx.local_time, lang=intent.lang)
        if _passes_hours(poi, verdict, intent):
            passed.append((i, poi, distance, verdict))

    # a POI without a vector gets the pool median instead of a lexical-only score: otherwise
    # it sits on another scale than 0.7*cos + 0.3*lex of its neighbours
    median: float | None = None
    if semantic is not None:
        finite = [float(semantic[i]) for i, *_ in passed if not np.isnan(semantic[i])]
        median = float(np.median(finite)) if finite else None
    pool: list[Candidate] = []
    for i, poi, distance, verdict in passed:
        sem: float | None = None
        info: dict[str, float] = {}
        if semantic is not None and not np.isnan(semantic[i]):
            sem = float(semantic[i])
            info["semantic"] = round(sem, 4)
        elif semantic is not None and median is not None:
            sem = median
            info["no_vector"] = 1.0
        score, parts = _score(poi, intent, ctx, distance, verdict, sem, query_stems)
        parts.update(info)
        pool.append(
            Candidate(
                poi=poi,
                distance_m=round(distance),
                walk_min=walk_minutes(distance),
                hours=verdict,
                score=score,
                score_parts=parts,
            )
        )
    pool.sort(key=lambda c: (-c.score, c.distance_m, c.poi.id))
    # one candidate per unnamed name: 110 of 111 playgrounds in OSM are «Детская площадка»
    generic_seen: set[str] = set()
    unique: list[Candidate] = []
    for cand in pool:
        key = generic_key(cand.poi)
        if key is not None:
            if key in generic_seen:
                continue
            generic_seen.add(key)
        unique.append(cand)
    chosen = _mmr(unique[: max(MMR_POOL, k)], k)
    for cand in chosen:
        cand.matched = explain(cand, intent, ctx)
    return chosen
