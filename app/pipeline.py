"""End-to-end recommendation flow. Owner: LLM lane (task T34).

recommend():
 1. resolve_origin(location)                       app.geo       (CORE)
 2. parse_query + apply_overrides                  app.query_parser
 3. context: local time, radius_for, weather       app.hours / app.geo / app.weather
 4. query_vec = llm.embed_query(...) or None
 5. retrieve(store, intent, ctx, query_vec, k=8)   app.retrieval (CORE)
    no candidates -> widen radius x1.5 once -> still none -> empty answer + helpful warning
    (LLM lane addition: before giving up, retry once without the category filter, with a
    warning — the airport zone has 3 cafes and no sights, S4 must not come back empty)
 6. draft_cards (LLM) -> finalize                  app.generate + app.validate (CORE)
    LLMUnavailable / use_llm=False -> template_cards
 7. plan_route for >= 2 cards when want_route      app.route (CORE)
    (T71: "прогулка на N минут" = walk tag + budget -> route.plan_walk picks <= 3 stops that
    fit the budget as a loop BEFORE the cards are written; the route closes on the start)
 8. DataUsed + warnings + disclaimer -> Answer
Every step degrades gracefully; never raise to the API except for invalid input.

Invalid input = InvalidRequest (unknown landmark id, invalid H3 cell); the API maps it to 422.
Privacy: an H3 cell finer than settings.h3_res is coarsened to its parent before use, and the
log line of a request carries only ids, counts and timings — never the query text or the cell.
CORE functions are called through their modules (`geo.resolve_origin`, ...) so tests can swap
them for fakes.
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

import h3
import numpy as np

from app import geo, hours, retrieval, route, validate, weather
from app.config import Settings
from app.generate import draft_cards
from app.llm import LLMClient, LLMUnavailable
from app.query_parser import apply_overrides, mentions_flight, parse_query
from app.schemas import (
    Answer,
    Candidate,
    Card,
    DataUsed,
    Landmark,
    Lang,
    LocationInput,
    MiniRoute,
    QueryIntent,
    RecommendRequest,
    SearchContext,
)
from app.store import Store

log = logging.getLogger(__name__)

DISCLAIMER_RU = (
    "Подсказки основаны на открытых данных (OpenStreetMap и др.) и могут устареть. "
    "Часы работы и доступность проверяйте перед визитом. Тексты «Идея ИИ» сгенерированы моделью."
)
DISCLAIMERS: dict[str, str] = {
    "ru": DISCLAIMER_RU,
    "kk": (
        "Ұсыныстар ашық деректерге (OpenStreetMap және т.б.) негізделген және ескіруі мүмкін. "
        "Жұмыс уақыты мен қолжетімділікті барар алдында тексеріңіз. «ЖИ идеясы» мәтіндерін "
        "модель жасаған."
    ),
    "en": (
        "Suggestions are based on open data (OpenStreetMap and others) and may be outdated. "
        "Check opening hours and accessibility before you go. “AI idea” texts are generated "
        "by a model."
    ),
}
TOP_K = 8
SPARSE_MIN = 3  # fewer candidates than this -> "few places nearby" warning
# Tourist before a flight (wave 3 rule): walk budget = time to departure - transfer - reserve.
# Applies only when a flight / the airport is mentioned or the UI sent departure_in_min.
AIRPORT_ID = "airport_nqz"
AIRPORT_TRANSFER_MIN = 35  # city centre -> NQZ by taxi: an estimate, said so in the warning
AIRPORT_RESERVE_MIN = 60  # check-in and security before departure
MIN_WALK_LEFT_MIN = 15  # less than this left -> no cards, "leave now"
TOURIST_MAX_CARDS = 2
HUB_MAX_WALK_MIN = 15  # at a transport hub and for tourists: short walks, no radius widening
CAREFUL_MAX_WALK_MIN = 15  # kids / wheelchair / night, unless the user set a limit
NIGHT_FROM_H, NIGHT_TO_H = 22, 6  # night mode by the clock (local time)
GROUP_MIN = 3
FOOD_CATEGORIES = frozenset({"cafe", "restaurant", "fast_food", "bar"})
MIN_WALK_BUDGET_MIN = 5  # QueryIntent.time_budget_min lower bound
MIN_YEAR, MAX_YEAR = 2000, 2100  # sane range for the demo "time machine" (`at`)
WIDEN_FACTOR = 1.5
MAX_RADIUS_M = 5000  # SearchContext.radius_m upper bound

TEXTS: dict[str, dict[str, str]] = {
    "ru": {
        "llm_down": "LLM недоступна — показаны карточки по данным.",
        "hours_unknown_1": "Для 1 места нет данных о часах работы — проверьте перед визитом.",
        "hours_unknown_n": "Для {n} мест нет данных о часах работы — проверьте перед визитом.",
        "night": "Ночь: выбирайте освещённые людные места; обратно лучше на такси.",
        "kids_night": "Уже поздно для прогулки с ребёнком — лучше место в помещении рядом.",
        "night_far": "Ночью в 15 минутах пешком нет мест, которые точно открыты, — "
                     "показаны ближайшие подтверждённые; лучше доехать на такси.",
        "group": "Для компании из {n} человек: столик в кафе лучше уточнить заранее.",
        "f_night_clock": "ночь (по времени)",
        "widened": "Рядом мало вариантов — радиус поиска расширен до {r} м.",
        "nothing": "Подходящих мест не нашлось. Попробуйте увеличить время, убрать часть "
                   "фильтров или выбрать другую точку.",
        "search_error": "Поиск временно дал сбой — попробуйте ещё раз.",
        "landmark_mismatch": "В запросе упомянут ориентир «{name}», но подборка построена "
                             "от выбранной точки.",
        "wheelchair": "Доступность для коляски подтверждена для {k} из {n} мест — уточните "
                      "перед визитом.",
        "tourist": "Заложите время на дорогу в аэропорт и регистрацию.",
        "tourist_hub": "Вы у транспортного узла: подборка — в пешей доступности, чтобы успеть "
                       "к отправлению.",
        "relaxed": "Под запрошенный тип мест рядом ничего нет в данных — показано, что есть "
                   "поблизости.",
        "sparse": "Рядом мало мест в открытых данных ({n}) — подборка короткая.",
        "airport_plan": "До вылета {b} мин: дорога до аэропорта ≈{tr} мин (оценка) и запас "
                        "{res} мин — на прогулку {w} мин. Выезжайте в аэропорт не позже {hhmm}.",
        "airport_now": "До вылета {b} мин: дорога до аэропорта ≈{tr} мин (оценка) и запас "
                       "{res} мин — на прогулку времени не остаётся. Выезжайте в аэропорт "
                       "сейчас.",
        "summary_leave_now": "Времени на прогулку перед вылетом нет — выезжайте в аэропорт "
                             "сейчас.",
        "airport_nothing": "Рядом с аэропортом почти ничего нет; ехать в город за {dur} до "
                           "вылета не советуем.",
        "airport_nothing_nodur": "Рядом с аэропортом почти ничего нет; ехать в город перед "
                                 "вылетом не советуем.",
        "airport_nothing_tail": "Ниже — что есть в пешей доступности.",
        "f_budget_airport": "время на прогулку {v} мин ({b} − {tr} дорога − {res} запас)",
        "route_over": "Маршрут не укладывается в заданное время — начните с первой карточки.",
        "summary_1": "Подборка по открытым данным: 1 идея рядом.",
        "summary_n": "Подборка по открытым данным: {n} идеи рядом.",
        "summary_empty": "Рядом не нашлось подходящих мест.",
        "weather_unknown": "погода неизвестна",
        "time_manual": "время задано вручную",
        "f_radius": "радиус {v} м", "f_budget": "время {v} мин", "f_walk": "пешком до {v} мин",
        "f_kids": "с ребёнком", "f_wheelchair": "доступная среда", "f_indoor": "в помещении",
        "f_outdoor": "на улице", "f_night": "ночь", "f_tourist": "турист",
        "f_group": "группа {v}", "f_categories": "категории: {v}", "f_tags": "теги: {v}",
        "f_no_ai": "режим без ИИ",
    },
    "kk": {
        "llm_down": "LLM қолжетімсіз — деректер бойынша карточкалар көрсетілді.",
        "hours_unknown_1": "1 орынның жұмыс уақыты туралы дерек жоқ — барар алдында тексеріңіз.",
        "hours_unknown_n": "{n} орынның жұмыс уақыты туралы дерек жоқ — барар алдында "
                           "тексеріңіз.",
        "night": "Түн: жарық әрі адамы көп жерлерді таңдаңыз; кері қарай таксимен қайтқан "
                 "дұрыс.",
        "kids_night": "Баламен серуендеуге кеш — жақын жердегі жабық орынды таңдаған дұрыс.",
        "night_far": "Түнде 15 минуттық жерде нақты ашық орын жоқ — ең жақын расталған "
                     "орындар көрсетілді; таксимен барған дұрыс.",
        "group": "{n} адамдық топқа: кафеде үстелді алдын ала нақтылаңыз.",
        "f_night_clock": "түн (уақыт бойынша)",
        "widened": "Жақын жерде нұсқа аз — іздеу радиусы {r} м-ге дейін ұлғайтылды.",
        "nothing": "Сәйкес орын табылмады. Уақытты көбейтіп, кейбір сүзгілерді алып тастап "
                   "немесе басқа нүкте таңдап көріңіз.",
        "search_error": "Іздеуде уақытша қате шықты — қайталап көріңіз.",
        "landmark_mismatch": "Сұрауда «{name}» аталған, бірақ іріктеме таңдалған нүктеден "
                             "жасалды.",
        "wheelchair": "Арбамен кіру {n} орынның {k}-інде ғана расталған — барар алдында "
                      "нақтылаңыз.",
        "tourist": "Әуежайға жол мен тіркеуге уақыт қалдырыңыз.",
        "tourist_hub": "Сіз көлік торабындасыз: жөнелтуге үлгеру үшін іріктеме жаяу "
                       "қашықтықта.",
        "relaxed": "Сұралған түрдегі орын жақын жерде деректерде жоқ — маңайдағы бар орындар "
                   "көрсетілді.",
        "sparse": "Ашық деректерде жақын жерде орын аз ({n}) — іріктеме қысқа.",
        "airport_plan": "Ұшуға {b} мин қалды: әуежайға жол ≈{tr} мин (болжам) және {res} мин "
                        "қор — серуенге {w} мин. Әуежайға {hhmm}-ден кешіктірмей шығыңыз.",
        "airport_now": "Ұшуға {b} мин қалды: әуежайға жол ≈{tr} мин (болжам) және {res} мин "
                       "қор — серуенге уақыт қалмайды. Әуежайға қазір шығыңыз.",
        "summary_leave_now": "Ұшар алдында серуенге уақыт жоқ — әуежайға қазір шығыңыз.",
        "airport_nothing": "Әуежай маңында ештеңе дерлік жоқ; ұшарға {dur} қалғанда қалаға "
                           "баруды ұсынбаймыз.",
        "airport_nothing_nodur": "Әуежай маңында ештеңе дерлік жоқ; ұшар алдында қалаға "
                                 "баруды ұсынбаймыз.",
        "airport_nothing_tail": "Төменде — жаяу жетуге болатын орындар.",
        "f_budget_airport": "серуенге {v} мин ({b} − {tr} жол − {res} қор)",
        "route_over": "Маршрут берілген уақытқа сыймайды — бірінші карточкадан бастаңыз.",
        "summary_1": "Ашық деректер бойынша жақын жерде 1 ұсыныс.",
        "summary_n": "Ашық деректер бойынша жақын жерде {n} ұсыныс.",
        "summary_empty": "Жақын жерде сәйкес орын табылмады.",
        "weather_unknown": "ауа райы белгісіз",
        "time_manual": "уақыт қолмен берілген",
        "f_radius": "радиус {v} м", "f_budget": "уақыт {v} мин",
        "f_walk": "жаяу {v} минутқа дейін", "f_kids": "баламен",
        "f_wheelchair": "кедергісіз орта", "f_indoor": "ғимарат ішінде", "f_outdoor": "далада",
        "f_night": "түн", "f_tourist": "турист", "f_group": "топ {v}",
        "f_categories": "санаттар: {v}", "f_tags": "тегтер: {v}", "f_no_ai": "ЖИ-сыз режим",
    },
    "en": {
        "llm_down": "The LLM is unavailable — showing cards built from the data.",
        "hours_unknown_1": "No opening-hours data for 1 place — check before you go.",
        "hours_unknown_n": "No opening-hours data for {n} places — check before you go.",
        "night": "Night: choose well-lit, busy places; better take a taxi back.",
        "kids_night": "It is late for a walk with a child — an indoor place nearby is better.",
        "night_far": "No surely-open place within a 15-minute walk tonight — showing the "
                     "nearest confirmed ones; better take a taxi.",
        "group": "For a group of {n}: check table availability at cafes in advance.",
        "f_night_clock": "night (by the clock)",
        "widened": "Few options nearby — search radius widened to {r} m.",
        "nothing": "No matching places found. Try more time, fewer filters or another "
                   "starting point.",
        "search_error": "Search failed temporarily — please try again.",
        "landmark_mismatch": "Your message mentions “{name}”, but results are built from the "
                             "selected starting point.",
        "wheelchair": "Wheelchair access is confirmed for {k} of {n} places — check before you go.",
        "tourist": "Leave time to get to the airport and check in.",
        "tourist_hub": "You are at a transport hub: picks are within walking distance so you "
                       "make your departure.",
        "relaxed": "Nothing of the requested type nearby in the data — showing what is around.",
        "sparse": "Few places nearby in the open data ({n}) — a short list.",
        "airport_plan": "{b} min to departure: ≈{tr} min to the airport (estimate) plus a "
                        "{res}-min reserve leaves {w} min for a walk. Leave for the airport no "
                        "later than {hhmm}.",
        "airport_now": "{b} min to departure: ≈{tr} min to the airport (estimate) plus a "
                       "{res}-min reserve leaves no time for a walk. Leave for the airport now.",
        "summary_leave_now": "There is no time for a walk before your flight — leave for the "
                             "airport now.",
        "airport_nothing": "There is almost nothing near the airport; we don't recommend going "
                           "into the city {dur} before your flight.",
        "airport_nothing_nodur": "There is almost nothing near the airport; we don't recommend "
                                 "going into the city right before your flight.",
        "airport_nothing_tail": "Below is what is within walking distance.",
        "f_budget_airport": "walk time {v} min ({b} − {tr} transfer − {res} reserve)",
        "route_over": "The route does not fit your time — start with the first card.",
        "summary_1": "1 nearby idea picked from open data.",
        "summary_n": "{n} nearby ideas picked from open data.",
        "summary_empty": "No suitable places nearby.",
        "weather_unknown": "weather unknown",
        "time_manual": "time set manually",
        "f_radius": "radius {v} m", "f_budget": "time {v} min", "f_walk": "walk up to {v} min",
        "f_kids": "with kids", "f_wheelchair": "wheelchair access", "f_indoor": "indoor",
        "f_outdoor": "outdoor", "f_night": "night", "f_tourist": "tourist",
        "f_group": "group of {v}", "f_categories": "categories: {v}", "f_tags": "tags: {v}",
        "f_no_ai": "no-AI mode",
    },
}


# T73: names in the "data used" panel follow the answer language (codes stay in Answer.intent).
CATEGORY_NAMES: dict[str, dict[str, str]] = {
    "ru": {"cafe": "кафе", "restaurant": "ресторан", "fast_food": "фастфуд", "bar": "бар",
           "nightlife": "ночная жизнь", "museum": "музей", "gallery": "галерея",
           "theatre": "театр", "cinema": "кино", "library": "библиотека", "park": "парк",
           "playground": "детская площадка", "viewpoint": "смотровая площадка",
           "attraction": "достопримечательность", "monument": "памятник",
           "religious": "культовое место", "mall": "ТРЦ", "sport": "спорт",
           "entertainment": "развлечения", "other": "другое"},
    "kk": {"cafe": "кафе", "restaurant": "мейрамхана", "fast_food": "фастфуд", "bar": "бар",
           "nightlife": "түнгі өмір", "museum": "мұражай", "gallery": "галерея",
           "theatre": "театр", "cinema": "кино", "library": "кітапхана", "park": "саябақ",
           "playground": "балалар алаңы", "viewpoint": "көру алаңы",
           "attraction": "көрікті жер", "monument": "ескерткіш", "religious": "ғибадат орны",
           "mall": "сауда орталығы", "sport": "спорт", "entertainment": "ойын-сауық",
           "other": "басқа"},
    "en": {"cafe": "cafe", "restaurant": "restaurant", "fast_food": "fast food", "bar": "bar",
           "nightlife": "nightlife", "museum": "museum", "gallery": "gallery",
           "theatre": "theatre", "cinema": "cinema", "library": "library", "park": "park",
           "playground": "playground", "viewpoint": "viewpoint", "attraction": "sight",
           "monument": "monument", "religious": "place of worship", "mall": "mall",
           "sport": "sport", "entertainment": "entertainment", "other": "other"},
}
TAG_NAMES: dict[str, dict[str, str]] = {
    "ru": {"coffee": "кофе", "wifi": "Wi-Fi", "power_outlets": "розетки", "quiet": "тихо",
           "work_friendly": "можно поработать", "kids": "для детей",
           "interactive": "интерактив", "indoor": "в помещении", "outdoor": "на улице",
           "view": "вид", "photo_spot": "фото", "night": "ночь", "late_open": "допоздна",
           "24_7": "круглосуточно", "free": "бесплатно", "wheelchair": "доступная среда",
           "vegetarian": "вегетарианское", "local_cuisine": "местная кухня",
           "live_music": "живая музыка", "walk": "прогулка", "water": "у воды",
           "architecture": "архитектура", "history": "история", "art": "искусство",
           "shopping": "шопинг", "sport": "спорт", "food": "еда"},
    "kk": {"coffee": "кофе", "wifi": "Wi-Fi", "power_outlets": "розетка", "quiet": "тыныш",
           "work_friendly": "жұмыс істеуге болады", "kids": "балаларға",
           "interactive": "интерактив", "indoor": "ғимарат ішінде", "outdoor": "далада",
           "view": "көрініс", "photo_spot": "фото", "night": "түн", "late_open": "кешке дейін",
           "24_7": "тәулік бойы", "free": "тегін", "wheelchair": "кедергісіз орта",
           "vegetarian": "вегетариандық", "local_cuisine": "ұлттық тағам",
           "live_music": "тірі музыка", "walk": "серуен", "water": "су жағасы",
           "architecture": "сәулет", "history": "тарих", "art": "өнер",
           "shopping": "сауда", "sport": "спорт", "food": "тамақ"},
    "en": {"coffee": "coffee", "wifi": "Wi-Fi", "power_outlets": "power outlets",
           "quiet": "quiet", "work_friendly": "work-friendly", "kids": "for kids",
           "interactive": "interactive", "indoor": "indoor", "outdoor": "outdoor",
           "view": "view", "photo_spot": "photo spot", "night": "night",
           "late_open": "open late", "24_7": "24/7", "free": "free",
           "wheelchair": "wheelchair access", "vegetarian": "vegetarian",
           "local_cuisine": "local cuisine", "live_music": "live music", "walk": "walk",
           "water": "waterside", "architecture": "architecture", "history": "history",
           "art": "art", "shopping": "shopping", "sport": "sport", "food": "food"},
}


class InvalidRequest(ValueError):
    """The request cannot be answered as given (the API answers 422 with this message)."""


class UnknownLandmarkError(InvalidRequest):
    pass


@dataclass
class Deps:
    store: Store
    llm: LLMClient | None
    settings: Settings


def recommend(req: RecommendRequest, deps: Deps) -> Answer:
    started = time.perf_counter()
    request_id = uuid.uuid4().hex[:12]
    store = deps.store
    llm = deps.llm if (req.use_llm and deps.llm is not None and deps.llm.available) else None
    warn: list[str] = []

    # 1. origin (invalid input is the only thing that may raise)
    location = _coarsen(req.location, deps.settings.h3_res)
    local_time = _local_time(req.at)
    if location.landmark_id is not None and location.landmark_id not in store.landmarks:
        raise UnknownLandmarkError(
            f"Неизвестный ориентир: {location.landmark_id!r}. Список: GET /api/landmarks"
        )
    try:
        origin, h3_cell, landmark_id, area_label = geo.resolve_origin(location, store.landmarks)
    except KeyError as exc:
        raise UnknownLandmarkError(f"Неизвестный ориентир: {location.landmark_id!r}") from exc

    # 2. intent
    intent = parse_query(req.query, req.lang, llm, store.landmarks)
    parse_model = llm.last_model if (llm is not None and intent.parser == "llm") else None
    intent = apply_overrides(intent, req.overrides)
    t = TEXTS[intent.lang]
    if req.use_llm and (deps.llm is None or not deps.llm.available):
        warn.append(t["llm_down"])
    if intent.landmark_mention and intent.landmark_mention != landmark_id:
        lm = store.landmarks.get(intent.landmark_mention)
        if lm is not None:
            name = lm.names.get(intent.lang) or next(iter(lm.names.values()), lm.id)
            warn.append(t["landmark_mismatch"].format(name=name))

    # Modes. `plan_intent` is what the search actually uses; `intent` stays what was asked.
    at_airport = landmark_id == AIRPORT_ID
    at_hub = landmark_id is not None and store.landmarks[landmark_id].kind == "transport"
    departure = getattr(req.overrides, "departure_in_min", None) if req.overrides else None
    flight = intent.tourist and not at_airport and (
        departure is not None or mentions_flight(req.query))
    walk_budget: int | None = None  # minutes left for the walk before leaving for the airport
    plan_update: dict[str, object] = {}
    if flight and intent.time_budget_min:
        walk_budget = intent.time_budget_min - AIRPORT_TRANSFER_MIN - AIRPORT_RESERVE_MIN
        plan_update["time_budget_min"] = max(MIN_WALK_BUDGET_MIN, walk_budget)
    short_walks = at_hub or intent.tourist
    if short_walks:
        plan_update["max_walk_min"] = min(intent.max_walk_min or HUB_MAX_WALK_MIN,
                                          HUB_MAX_WALK_MIN)
    # Night by the clock (22:00–06:00), unless the UI switched night mode off explicitly:
    # retrieval then keeps late_open / 24_7 / open-now places only.
    night_switch = getattr(req.overrides, "night", None) if req.overrides else None
    night_by_clock = (not intent.night and night_switch is None
                      and (local_time.hour >= NIGHT_FROM_H or local_time.hour < NIGHT_TO_H))
    if night_by_clock:
        plan_update["night"] = True
    careful = intent.with_kids or intent.wheelchair or intent.night or night_by_clock
    careful_cap = careful and intent.max_walk_min is None and "max_walk_min" not in plan_update
    if careful_cap:
        plan_update["max_walk_min"] = CAREFUL_MAX_WALK_MIN
    plan_intent = intent.model_copy(update=plan_update) if plan_update else intent
    no_time = walk_budget is not None and walk_budget < MIN_WALK_LEFT_MIN

    # 3. context (the area label is in the answer language: the LLM and the panel read it)
    lm_here = store.landmarks.get(landmark_id) if landmark_id else None
    area_label = _localized_area(intent.lang, lm_here, h3_cell, area_label)
    wx = weather.get_weather(origin, local_time)
    ctx = SearchContext(
        origin=origin,
        h3_cell=h3_cell,
        h3_res=h3.get_resolution(h3_cell),
        area_label=area_label,
        landmark_id=landmark_id,
        local_time=local_time,
        weather=wx,
        radius_m=_safe_radius(plan_intent),
    )

    # 4. query vector (Gemini, optional; never in the no-AI mode)
    query_vec = None
    if req.use_llm and deps.llm is not None and store.embeddings is not None:
        query_vec = deps.llm.embed_query(intent.raw_query)

    # 5. retrieve: as asked -> radius x1.5 once -> without the category filter (sparse areas,
    #    e.g. 3 cafes and no sights around the airport). `search_intent` is what was applied.
    search_intent = plan_intent
    if no_time:  # nothing fits before the flight: do not search, do not spend the LLM
        candidates, failed = [], False
    else:
        candidates, failed = _retrieve(store, search_intent, ctx, query_vec)
    if short_walks:  # "two cards beat a third one 48 minutes away"
        candidates = [c for c in candidates if c.walk_min <= HUB_MAX_WALK_MIN]
    if not candidates and not failed and not no_time and not short_walks:
        wider = min(round(ctx.radius_m * WIDEN_FACTOR), MAX_RADIUS_M)
        if wider > ctx.radius_m:
            ctx = ctx.model_copy(update={"radius_m": wider})
            candidates, failed = _retrieve(store, search_intent, ctx, query_vec)
            if candidates:
                warn.append(t["widened"].format(r=wider))
    if plan_intent.night and not no_time:
        # At night "surely open" beats "close": keep open / late_open / 24_7 places; if none
        # is within the short-walk cap, look further (the taxi back is advised anyway).
        sure = [c for c in candidates if _night_sure(c)]
        if not sure and careful_cap and not short_walks and not failed:
            far_intent = plan_intent.model_copy(update={"max_walk_min": intent.max_walk_min})
            far_ctx = ctx.model_copy(update={"radius_m": min(
                round(_safe_radius(far_intent) * WIDEN_FACTOR), MAX_RADIUS_M)})
            more, failed = _retrieve(store, far_intent, far_ctx, query_vec)
            sure = [c for c in more if _night_sure(c)]
            if sure:
                search_intent, ctx = far_intent, far_ctx
                warn.append(t["night_far"])
        if sure:
            candidates = sure
    requested_found = len(candidates)  # places of the requested type, before relaxing
    if not candidates and not failed and not no_time and plan_intent.categories:
        search_intent = plan_intent.model_copy(update={"categories": []})
        candidates, failed = _retrieve(store, search_intent, ctx, query_vec)
        if short_walks:
            candidates = [c for c in candidates if c.walk_min <= HUB_MAX_WALK_MIN]
        if plan_intent.night:
            candidates = [c for c in candidates if _night_sure(c)] or candidates
        if candidates:
            warn.append(t["relaxed"])
    if 0 < len(candidates) < SPARSE_MIN:
        warn.append(t["sparse"].format(n=len(candidates)))

    # T71: a walk of N minutes -> pick stops that fit together as a loop, then write them up
    walk_loop = (bool(plan_intent.time_budget_min) and "walk" in plan_intent.must_tags
                 and not intent.tourist and not plan_intent.night)
    gen_candidates = candidates
    if walk_loop and req.want_route and len(candidates) >= 2:
        picked = route.plan_walk(ctx.origin, candidates, plan_intent.time_budget_min or 0)
        if len(picked) >= 2:
            gen_candidates = picked

    # 6. cards
    cards: list[Card] = []
    generation = "template"
    summary: str | None = None
    gen_model: str | None = None
    if candidates and llm is not None:
        try:
            draft = draft_cards(search_intent, ctx, gen_candidates, llm)
            cards, fin_warnings = validate.finalize(draft, gen_candidates, search_intent, ctx)
            warn.extend(fin_warnings)
            draft_ids = {d.poi_id for d in draft.cards}
            if any(c.poi_id in draft_ids for c in cards):
                generation, summary, gen_model = "llm", draft.summary, llm.last_model
        except LLMUnavailable as exc:
            log.info("request %s: generation fell back to templates (%s)", request_id,
                     type(exc).__name__)
            warn.append(t["llm_down"])
            cards = []
        except Exception as exc:  # noqa: BLE001 - finalize bug must not kill the answer
            log.error("request %s: finalize failed (%s)", request_id, type(exc).__name__)
            cards = []
    if candidates and not cards:
        try:
            cards = validate.template_cards(gen_candidates, search_intent, ctx)
        except Exception as exc:  # noqa: BLE001
            log.error("request %s: template_cards failed (%s)", request_id, type(exc).__name__)
            failed = True
    cards = cards[: TOURIST_MAX_CARDS if walk_budget is not None else 3]
    cards = _localize_hours(cards, candidates, local_time, intent.lang)

    # 7. route
    note = route.route_note(intent.lang, loop=walk_loop, kids=intent.with_kids,
                            wheelchair=intent.wheelchair)
    # At a transport hub people pick one place to wait: a tour of three cafes makes no sense.
    mini_route = _route(ctx, cards, candidates, search_intent, req.want_route and not at_hub,
                        request_id, loop=walk_loop, note=note)

    # 8. warnings, data used, answer
    if failed and not cards:
        warn.append(t["search_error"])
    elif not cards and not no_time:
        warn.append(t["nothing"])
    if walk_budget is not None:
        warn.append(_airport_warning(t, intent.time_budget_min or 0, walk_budget, local_time))
    warn.extend(_context_warnings(t, plan_intent, cards, candidates, mini_route, at_hub=at_hub,
                                  flight=flight, deadline_given=walk_budget is not None))
    if summary is None:
        summary = _template_summary(t, len(cards))
    if no_time:
        summary = t["summary_leave_now"]
    elif at_airport and intent.tourist and requested_found < 2:
        summary = _airport_summary(t, intent, has_cards=bool(cards))
    summary = validate.sanitize_summary(summary, cards)  # "open now" never leaks via summary

    answer = Answer(
        request_id=request_id,
        lang=intent.lang,
        summary=summary,
        cards=cards,
        route=mini_route,
        intent=intent,
        context=ctx,
        data_used=DataUsed(
            location=area_label if landmark_id else f"{area_label} · H3 r{ctx.h3_res}",
            time=_time_label(t, local_time, manual=req.at is not None),
            weather=(weather.summary_for(wx, intent.lang) if wx is not None
                     else t["weather_unknown"]),
            filters=_filters(t, search_intent, ctx, req.use_llm, lang=intent.lang,
                             night_by_clock=night_by_clock,
                             airport=(intent.time_budget_min or 0) if walk_budget is not None
                             else None),
            sources=_sources(cards, wx is not None),
            candidates_considered=len(candidates),
            parser=intent.parser,
            generation=generation,
            model=gen_model or parse_model,
        ),
        warnings=list(dict.fromkeys(warn)),
        disclaimer=DISCLAIMERS[intent.lang],
        latency_ms=round((time.perf_counter() - started) * 1000),
    )
    log.info(
        "recommend id=%s res=%d candidates=%d cards=%d parser=%s generation=%s ms=%d",
        request_id, ctx.h3_res, len(candidates), len(cards), intent.parser, generation,
        answer.latency_ms,
    )
    return answer


# -------------------------------------------------------------------- helpers


def _night_sure(c: Candidate) -> bool:
    """Open now by the data, or marked as working late / round the clock."""
    return c.hours.status == "open" or bool({"late_open", "24_7"} & set(c.poi.tags))


def _localized_area(lang: Lang, landmark: Landmark | None, cell: str, ru_label: str) -> str:
    """CORE builds the Russian label («у EXPO», «ваш район (≈0,1 км²) у …»); kk / en here."""
    if lang == "ru":
        return ru_label
    if landmark is not None:
        name = landmark.names.get(lang) or landmark.names.get("ru") or landmark.id
        return f"near {name}" if lang == "en" else f"{name} маңында"
    area = f"{h3.cell_area(cell, unit='km^2'):.1f}"
    if lang == "en":
        return f"your area (≈{area} km²)"
    return f"сіздің ауданыңыз (≈{area.replace('.', ',')} км²)"


def _localize_hours(cards: list[Card], candidates: list[Candidate], at: datetime, lang: Lang
                    ) -> list[Card]:
    """Hours notes in kk / en (texts live in app.hours). Status and confidence are untouched:
    a card is only re-worded when hours.py gives the very same verdict."""
    if lang == "ru":
        return cards
    by_id = {c.poi.id: c for c in candidates}
    out: list[Card] = []
    for card in cards:
        cand = by_id.get(card.poi_id)
        if cand is None:
            out.append(card)
            continue
        verdict = hours.evaluate_hours(cand.poi.opening_hours, at, lang=lang)
        if (verdict.status, verdict.confidence) != (card.open_status, card.open_confidence):
            out.append(card)
            continue
        raw = (cand.poi.opening_hours or "").strip()
        note = hours.unknown_note(lang) if verdict.status == "unknown" and not raw else verdict.note
        out.append(card.model_copy(update={"open_note": note}))
    return out


def _coarsen(loc: LocationInput, res: int) -> LocationInput:
    if loc.h3_cell is None:
        return loc
    if not h3.is_valid_cell(loc.h3_cell):
        raise InvalidRequest("h3_cell не является корректной H3-ячейкой")
    if h3.get_resolution(loc.h3_cell) > res:
        return LocationInput(h3_cell=h3.cell_to_parent(loc.h3_cell, res))
    return loc


def _local_time(at: datetime | None) -> datetime:
    if at is None:
        return hours.now_astana()
    if not MIN_YEAR <= at.year <= MAX_YEAR:  # year 1 / 9999 overflow tz and timedelta math
        raise InvalidRequest(f"at вне допустимого диапазона ({MIN_YEAR}–{MAX_YEAR})")
    if at.tzinfo is None:
        return at.replace(tzinfo=hours.ASTANA_TZ)
    return at.astimezone(hours.ASTANA_TZ)


def _safe_radius(intent: QueryIntent) -> int:
    try:
        radius = int(geo.radius_for(intent))
    except Exception as exc:  # noqa: BLE001
        log.error("radius_for failed (%s), using default", type(exc).__name__)
        radius = geo.DEFAULT_RADIUS_M
    return max(100, min(MAX_RADIUS_M, radius))


def _retrieve(
    store: Store, intent: QueryIntent, ctx: SearchContext, query_vec: np.ndarray | None
) -> tuple[list[Candidate], bool]:
    try:
        return retrieval.retrieve(store, intent, ctx, query_vec, k=TOP_K), False
    except Exception as exc:  # noqa: BLE001 - an empty answer beats a 500
        log.error("retrieve failed (%s)", type(exc).__name__)
        return [], True


def _route(
    ctx: SearchContext,
    cards: list[Card],
    candidates: list[Candidate],
    intent: QueryIntent,
    want_route: bool,
    request_id: str,
    *,
    loop: bool = False,
    note: str | None = None,
) -> MiniRoute | None:
    if not want_route or len(cards) < 2:
        return None
    by_id = {c.poi.id: c for c in candidates}
    stops = [by_id[c.poi_id] for c in cards if c.poi_id in by_id]
    try:
        return route.plan_route(
            ctx.origin, stops, intent.time_budget_min, {c.poi_id: c.visit_min_low for c in cards},
            loop=loop, note=note,
        )
    except Exception as exc:  # noqa: BLE001 - the cards are still useful without a route
        log.error("request %s: plan_route failed (%s)", request_id, type(exc).__name__)
        return None


def _context_warnings(
    t: dict[str, str],
    intent: QueryIntent,
    cards: list[Card],
    candidates: list[Candidate],
    mini_route: MiniRoute | None,
    *,
    at_hub: bool = False,
    flight: bool = False,
    deadline_given: bool = False,
) -> list[str]:
    out: list[str] = []
    unknown = sum(1 for c in cards if c.open_status == "unknown")
    if unknown == 1:
        out.append(t["hours_unknown_1"])
    elif unknown > 1:
        out.append(t["hours_unknown_n"].format(n=unknown))
    if intent.night:
        out.append(t["night"])
        if intent.with_kids:
            out.append(t["kids_night"])
    if intent.wheelchair and cards:
        wheel = {c.poi.id: c.poi.wheelchair for c in candidates}
        confirmed = sum(1 for c in cards if wheel.get(c.poi_id) == "yes")
        if confirmed < len(cards):  # OSM marks wheelchair for ~1% of places
            out.append(t["wheelchair"].format(k=confirmed, n=len(cards)))
    if intent.tourist and cards and not deadline_given:
        if at_hub:
            out.append(t["tourist_hub"])
        elif flight:  # a tourist without a flight gets no airport advice
            out.append(t["tourist"])
    if (intent.group_size or 0) >= GROUP_MIN and any(c.category in FOOD_CATEGORIES
                                                      for c in cards):
        out.append(t["group"].format(n=intent.group_size))
    if mini_route is not None and not mini_route.fits_budget:
        out.append(t["route_over"])
    return out


def _airport_warning(t: dict[str, str], budget: int, walk_budget: int, local_time: datetime
                     ) -> str:
    """The calculation is shown, not just the result: «до вылета 120 − 35 − 60 = 25 мин»."""
    fmt = {"b": budget, "tr": AIRPORT_TRANSFER_MIN, "res": AIRPORT_RESERVE_MIN}
    if walk_budget < MIN_WALK_LEFT_MIN:
        return t["airport_now"].format(**fmt)
    leave_at = local_time + timedelta(minutes=walk_budget)
    return t["airport_plan"].format(**fmt, w=walk_budget, hhmm=f"{leave_at:%H:%M}")


def _airport_summary(t: dict[str, str], intent: QueryIntent, *, has_cards: bool) -> str:
    dur = _duration(intent.time_budget_min, intent.lang)
    honest = t["airport_nothing"].format(dur=dur) if dur else t["airport_nothing_nodur"]
    return f"{honest} {t['airport_nothing_tail']}" if has_cards else honest


def _duration(minutes: int | None, lang: str) -> str | None:
    """'2 часа' / '1,5 часа' / '45 мин' (ru), '2 сағат' (kk), '2 hours' (en)."""
    if not minutes:
        return None
    if minutes % 30 != 0:
        return {"ru": f"{minutes} мин", "kk": f"{minutes} минут", "en": f"{minutes} minutes"}[lang]
    hours_ = minutes / 60
    num = f"{hours_:g}"
    if lang == "kk":
        return f"{num.replace('.', ',')} сағат"
    if lang == "en":
        return f"{num} hour" if hours_ == 1 else f"{num} hours"
    if not hours_.is_integer():
        return f"{num.replace('.', ',')} часа"
    h = int(hours_)
    if h % 10 == 1 and h % 100 != 11:
        return f"{h} час"
    if h % 10 in (2, 3, 4) and h % 100 not in (12, 13, 14):
        return f"{h} часа"
    return f"{h} часов"


def _template_summary(t: dict[str, str], n: int) -> str:
    if n == 0:
        return t["summary_empty"]
    return t["summary_1"] if n == 1 else t["summary_n"].format(n=n)


def _time_label(t: dict[str, str], local_time: datetime, *, manual: bool) -> str:
    label = f"{local_time:%d.%m.%Y %H:%M} (UTC+5)"
    return f"{label} · {t['time_manual']}" if manual else label


def _filters(t: dict[str, str], intent: QueryIntent, ctx: SearchContext, use_llm: bool,
             *, lang: Lang = "ru", airport: int | None = None,
             night_by_clock: bool = False) -> list[str]:
    """`airport` = minutes to departure when the flight rule shaped the time budget."""
    out = [t["f_radius"].format(v=ctx.radius_m)]
    if intent.time_budget_min and airport is not None:
        out.append(t["f_budget_airport"].format(v=intent.time_budget_min, b=airport,
                                                tr=AIRPORT_TRANSFER_MIN, res=AIRPORT_RESERVE_MIN))
    elif intent.time_budget_min:
        out.append(t["f_budget"].format(v=intent.time_budget_min))
    if intent.max_walk_min:
        out.append(t["f_walk"].format(v=intent.max_walk_min))
    flags = (("f_kids", intent.with_kids), ("f_wheelchair", intent.wheelchair),
             ("f_indoor", intent.indoor == "indoor"), ("f_outdoor", intent.indoor == "outdoor"),
             ("f_night_clock" if night_by_clock else "f_night", intent.night),
             ("f_tourist", intent.tourist))
    out.extend(t[key] for key, on in flags if on)
    if intent.group_size:
        out.append(t["f_group"].format(v=intent.group_size))
    if intent.categories:
        names = CATEGORY_NAMES[lang]
        out.append(t["f_categories"].format(v=", ".join(names.get(c, c)
                                                         for c in intent.categories)))
    if intent.must_tags:
        tags = TAG_NAMES[lang]
        out.append(t["f_tags"].format(v=", ".join(tags.get(x, x) for x in intent.must_tags)))
    if not use_llm:
        out.append(t["f_no_ai"])
    return out


def _sources(cards: list[Card], has_weather: bool) -> list[str]:
    sources = sorted({s for c in cards for s in c.facts_sources})
    if has_weather:
        sources.append("open_meteo")
    return sources
