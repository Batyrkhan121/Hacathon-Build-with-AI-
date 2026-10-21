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

import inspect
import json
import logging
import math
import re
import time
import traceback
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal

import h3
import numpy as np

from app import events, geo, hours, retrieval, route, validate, walkgraph, weather
from app.config import Settings
from app.generate import draft_cards
from app.llm import LLMClient, LLMUnavailable
from app.query_parser import (
    apply_overrides,
    departure_phrase,
    explicit_categories,
    mentions_flight,
    parse_query,
)
from app.schemas import (
    POI,
    PREF_LABELS_RU,
    TAG_VOCAB,
    Answer,
    Candidate,
    Card,
    DataUsed,
    HoursVerdict,
    Landmark,
    Lang,
    LocationInput,
    MiniRoute,
    QueryIntent,
    RecommendRequest,
    RouteProfile,
    SearchContext,
    UserPrefs,
    WeatherInfo,
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
# No query vector (Gemini quota / failure) but Claude will write the cards: it gets a wider
# keyword pool and picks 1–3 by meaning (finalize still drops foreign ids, facts stay ours).
LLM_POOL_K = 16
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
MAX_ONE_WAY_WALK_MIN = 20  # budget-derived walk to one stop, unless the user set a limit
NIGHT_FROM_H, NIGHT_TO_H = 22, 6  # night mode by the clock (local time)
GROUP_MIN = 3
FOOD_CATEGORIES = frozenset({"cafe", "restaurant", "fast_food", "bar"})
EVENT_NIGHT_SOON_MIN = 60  # at night an event counts as «surely on» if it is on or this close
# T133: an event is a route stop only when it starts within this many minutes (or is on); then
# it is the last stop (route.plan_route(last_stop_id=...)). Later: a card, not a stop.
EVENT_STOP_MAX_WAIT_MIN = 30
SIGHT_CATEGORIES = frozenset({"attraction", "monument", "museum", "viewpoint", "gallery",
                              "religious", "park", "theatre"})
MIN_WALK_BUDGET_MIN = 5  # QueryIntent.time_budget_min lower bound
MIN_YEAR, MAX_YEAR = 2000, 2100  # sane range for the demo "time machine" (`at`)
WIDEN_FACTOR = 1.5
MAX_RADIUS_M = 5000  # SearchContext.radius_m upper bound
# T123 «в тепле» (warm mode): prefs likes "warm", the UI switch indoor_only, or frost by the
# weather. Stops are indoor (warm stops — retrieval.WARM_CATEGORIES — first), each within
# WARM_MAX_LEG_MIN of the start and of each other, so route.max_outdoor_leg_min stays short
# (E15: ≤ 10). No walk loop: the person stays in the warm, a place at the start is a stop too.
WARM_MAX_LEG_MIN = 10
FROST_C = -10.0  # air temperature by Open-Meteo at or below this is «мороз»; wind not counted
WARM_MIN_INDOOR = 2  # fewer indoor candidates than this -> outdoor places come back, said so
WARM_DROP_TAGS = frozenset({"walk", "outdoor"})  # «погулять» in the warm is not a park walk

TEXTS: dict[str, dict[str, str]] = {
    "ru": {
        "llm_down": "LLM недоступна — показаны карточки по данным.",
        "hours_unknown_1": "Для 1 места нет данных о часах работы — проверьте перед визитом.",
        "hours_unknown_n": "Для {n} мест нет данных о часах работы — проверьте перед визитом.",
        "night": "Ночь: выбирайте освещённые людные места; обратно лучше на такси.",
        "kids_night": "Уже поздно для прогулки с ребёнком — лучше место в помещении рядом.",
        "night_far": "Ночью в 15 минутах пешком нет мест, которые точно открыты, — "
                     "показаны ближайшие подтверждённые; лучше доехать на такси.",
        "night_far_open": "Ночью в 15 минутах пешком нет мест, которые точно открыты, — "
                          "показаны ближайшие подтверждённые.",
        "night_taxi": "Ночью до этих мест дольше {v} мин пешком — пеший маршрут не строим, "
                      "туда и обратно лучше на такси; выбирайте освещённые людные места.",
        "group": "Для компании из {n} человек: столик в кафе лучше уточнить заранее.",
        "f_night_clock": "ночь (по времени)",
        "widened": "Рядом мало вариантов — радиус поиска расширен до {r} м.",
        "nothing": "Подходящих мест не нашлось. Попробуйте увеличить время, убрать часть "
                   "фильтров или выбрать другую точку.",
        "search_error": "Поиск дал сбой на нашей стороне — повтор не поможет. Попробуйте "
                        "другой запрос или точку.",
        "llm_down_r": "LLM недоступна ({reason}) — показаны карточки по данным.",
        "parse_fallback": "Запрос разобран по правилам: ИИ не ответил ({reason}).",
        "gen_check_failed": "Тексты ИИ не прошли проверку — показаны карточки по данным.",
        "route_failed": "Маршрут построить не удалось — карточки показаны без маршрута.",
        "semantic_down": "Смысловой поиск недоступен ({reason}) — поиск только по словам.",
        "semantic_index_mismatch": "Смысловой поиск недоступен: индекс построен другой "
                                   "моделью — поиск только по словам.",
        "semantic_index_missing": "Смысловой поиск недоступен: индекс эмбеддингов не "
                                  "загружен — поиск только по словам.",
        "weather_manual": "погода не учтена: время задано вручную",
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
        "no_match": "Точного совпадения по запросу нет — показано, что есть рядом.",
        "graph_over_limit": "По тротуарам до мест дольше {v} мин пешком — показаны "
                            "ближайшие из найденных.",
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
        "weather_no_answer": "погода: нет данных (сервис не ответил)",
        "time_manual": "время задано вручную",
        "f_radius": "радиус {v} м", "f_budget": "время {v} мин", "f_walk": "пешком до {v} мин",
        "f_kids": "с ребёнком", "f_wheelchair": "доступная среда", "f_indoor": "в помещении",
        "f_outdoor": "на улице", "f_night": "ночь", "f_tourist": "турист",
        "f_group": "группа {v}", "f_categories": "категории: {v}", "f_tags": "теги: {v}",
        "f_no_ai": "режим без ИИ",
        "f_search_semantic": "поиск: по смыслу и по словам",
        "f_search_lexical": "поиск: по словам", "f_search_claude": ", отбор — Claude",
        "index_stale": "индекс устарел",
        "f_hours_dropped": "по часам работы отсеяно {n}: {parts}",
        "f_profile_accessible": "маршрут: без ступенек",
        "f_profile_lit": "маршрут: ночью — по крупным улицам",
        "f_profile_auto": " (коляска)",
        "f_profile_req_accessible": "запрошен маршрут без ступенек",
        "f_profile_req_lit": "запрошен маршрут «ночью — по крупным улицам»",
        "f_warm_between": "в тепле: между местами до {v} мин пешком",
        "f_short": "без долгих отрезков на улице: до {v} мин подряд",
        "f_events_open": "события: без фильтра по категориям мест",
        "f_short_between": "без долгих отрезков на улице: между местами до {v} мин",
        "short_long_leg": "Самый длинный отрезок по улице — {n} мин: ближе мест в "
                          "данных нет.",
        "f_prefs_likes": "предпочтения: {v}", "f_prefs_avoids": "избегать: {v}",
        "f_excluded": "без уже показанных мест: {n}",
        "summary_no_more": "Других подходящих мест рядом нет: уже показанные не "
                           "повторяем.",
        "f_warm": "в тепле: на улице до {v} мин подряд", "f_warm_frost": " (мороз {t})",
        "warm_outdoor": "Рядом мало мест в помещении — показаны и места на улице.",
        "warm_long_leg": "Самый длинный отрезок по улице — {n} мин: тёплых мест ближе "
                         "в данных нет, оденьтесь теплее.",
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
        "night_far_open": "Түнде 15 минуттық жерде нақты ашық орын жоқ — ең жақын расталған "
                          "орындар көрсетілді.",
        "night_taxi": "Түнде бұл орындарға жаяу {v} минуттан ұзақ — жаяу маршрут құрмаймыз, "
                      "барып-қайту үшін такси дұрыс; жарық әрі адамы көп жерлерді таңдаңыз.",
        "group": "{n} адамдық топқа: кафеде үстелді алдын ала нақтылаңыз.",
        "f_night_clock": "түн (уақыт бойынша)",
        "widened": "Жақын жерде нұсқа аз — іздеу радиусы {r} м-ге дейін ұлғайтылды.",
        "nothing": "Сәйкес орын табылмады. Уақытты көбейтіп, кейбір сүзгілерді алып тастап "
                   "немесе басқа нүкте таңдап көріңіз.",
        "search_error": "Іздеу біздің тарапта ақауға ұшырады — қайталау көмектеспейді. Басқа "
                        "сұрау немесе нүкте таңдап көріңіз.",
        "llm_down_r": "LLM қолжетімсіз ({reason}) — деректер бойынша карточкалар көрсетілді.",
        "parse_fallback": "Сұрау ережелер бойынша талданды: ЖИ жауап бермеді ({reason}).",
        "gen_check_failed": "ЖИ мәтіндері тексеруден өтпеді — деректер бойынша карточкалар "
                            "көрсетілді.",
        "route_failed": "Маршрут құру мүмкін болмады — карточкалар маршрутсыз көрсетілді.",
        "semantic_down": "Мағыналық іздеу қолжетімсіз ({reason}) — тек сөздер бойынша іздеу.",
        "semantic_index_mismatch": "Мағыналық іздеу қолжетімсіз: индекс басқа модельмен "
                                   "құрылған — тек сөздер бойынша іздеу.",
        "semantic_index_missing": "Мағыналық іздеу қолжетімсіз: эмбеддинг индексі жүктелмеген "
                                  "— тек сөздер бойынша іздеу.",
        "weather_manual": "ауа райы ескерілмеді: уақыт қолмен берілген",
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
        "no_match": "Сұрауға дәл сәйкес орын жоқ — жақын маңдағы орындар көрсетілді.",
        "graph_over_limit": "Тротуарлармен орындарға дейін {v} минуттан ұзақ жаяу — "
                            "табылғандардың ең жақындары көрсетілді.",
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
        "weather_no_answer": "ауа райы: дерек жоқ (қызмет жауап бермеді)",
        "time_manual": "уақыт қолмен берілген",
        "f_radius": "радиус {v} м", "f_budget": "уақыт {v} мин",
        "f_walk": "жаяу {v} минутқа дейін", "f_kids": "баламен",
        "f_wheelchair": "кедергісіз орта", "f_indoor": "ғимарат ішінде", "f_outdoor": "далада",
        "f_night": "түн", "f_tourist": "турист", "f_group": "топ {v}",
        "f_categories": "санаттар: {v}", "f_tags": "тегтер: {v}", "f_no_ai": "ЖИ-сыз режим",
        "f_search_semantic": "іздеу: мағынасы мен сөздері бойынша",
        "f_search_lexical": "іздеу: сөздері бойынша", "f_search_claude": ", іріктеу — Claude",
        "index_stale": "индекс ескірген",
        "f_hours_dropped": "жұмыс уақыты бойынша алынып тасталды {n}: {parts}",
        "f_profile_accessible": "маршрут: баспалдақсыз",
        "f_profile_lit": "маршрут: түнде — үлкен көшелермен",
        "f_profile_auto": " (арба)",
        "f_profile_req_accessible": "баспалдақсыз маршрут сұралды",
        "f_profile_req_lit": "«түнде — үлкен көшелермен» маршруты сұралды",
        "f_warm_between": "жылыда: орындар арасы {v} минутқа дейін жаяу",
        "f_short": "далада ұзақ жүрмей: қатарынан {v} минутқа дейін",
        "f_events_open": "іс-шаралар: орын санаттары бойынша сүзгісіз",
        "f_short_between": "далада ұзақ жүрмей: орындар арасы {v} минутқа дейін",
        "short_long_leg": "Көшедегі ең ұзақ кесінді — {n} мин: деректерде жақынырақ "
                          "орын жоқ.",
        "f_prefs_likes": "қалауыңыз: {v}", "f_prefs_avoids": "аулақ болу: {v}",
        "f_excluded": "көрсетілген орындарсыз: {n}",
        "summary_no_more": "Жақын жерде басқа лайықты орын жоқ: көрсетілгендерін "
                           "қайталамаймыз.",
        "f_warm": "жылыда: далада қатарынан {v} минутқа дейін",
        "f_warm_frost": " (аяз {t})",
        "warm_outdoor": "Жақын жерде жабық орындар аз — далада орындар да көрсетілді.",
        "warm_long_leg": "Көшедегі ең ұзақ кесінді — {n} мин: деректерде жақынырақ жылы "
                         "орын жоқ, жылырақ киініңіз.",
    },
    "en": {
        "llm_down": "The LLM is unavailable — showing cards built from the data.",
        "hours_unknown_1": "No opening-hours data for 1 place — check before you go.",
        "hours_unknown_n": "No opening-hours data for {n} places — check before you go.",
        "night": "Night: choose well-lit, busy places; better take a taxi back.",
        "kids_night": "It is late for a walk with a child — an indoor place nearby is better.",
        "night_far": "No surely-open place within a 15-minute walk tonight — showing the "
                     "nearest confirmed ones; better take a taxi.",
        "night_far_open": "No surely-open place within a 15-minute walk tonight — showing the "
                          "nearest confirmed ones.",
        "night_taxi": "At night these places are over {v} min on foot — no walking route; "
                      "better take a taxi there and back, and choose well-lit, busy places.",
        "group": "For a group of {n}: check table availability at cafes in advance.",
        "f_night_clock": "night (by the clock)",
        "widened": "Few options nearby — search radius widened to {r} m.",
        "nothing": "No matching places found. Try more time, fewer filters or another "
                   "starting point.",
        "search_error": "Search failed on our side — retrying will not help. Try another "
                        "request or starting point.",
        "llm_down_r": "The LLM is unavailable ({reason}) — showing cards built from the data.",
        "parse_fallback": "The request was parsed by rules: the AI did not answer ({reason}).",
        "gen_check_failed": "The AI texts failed the check — showing cards built from the data.",
        "route_failed": "Could not build the route — showing the cards without it.",
        "semantic_down": "Semantic search is unavailable ({reason}) — keyword search only.",
        "semantic_index_mismatch": "Semantic search is unavailable: the index was built with "
                                   "another model — keyword search only.",
        "semantic_index_missing": "Semantic search is unavailable: the embeddings index is not "
                                  "loaded — keyword search only.",
        "weather_manual": "weather not considered: time set manually",
        "landmark_mismatch": "Your message mentions “{name}”, but results are built from the "
                             "selected starting point.",
        "wheelchair": "Wheelchair access is confirmed for {k} of {n} places — check before you go.",
        "tourist": "Leave time to get to the airport and check in.",
        "tourist_hub": "You are at a transport hub: picks are within walking distance so you "
                       "make your departure.",
        "relaxed": "Nothing of the requested type nearby in the data — showing what is around.",
        "sparse": "Few places nearby in the open data ({n}) — a short list.",
        "no_match": "No exact match for your request — showing what is nearby.",
        "graph_over_limit": "Along the footways the places are over {v} min on foot — "
                            "the nearest found are shown.",
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
        "weather_no_answer": "weather: no data (the service did not answer)",
        "time_manual": "time set manually",
        "f_radius": "radius {v} m", "f_budget": "time {v} min", "f_walk": "walk up to {v} min",
        "f_kids": "with kids", "f_wheelchair": "wheelchair access", "f_indoor": "indoor",
        "f_outdoor": "outdoor", "f_night": "night", "f_tourist": "tourist",
        "f_group": "group of {v}", "f_categories": "categories: {v}", "f_tags": "tags: {v}",
        "f_no_ai": "no-AI mode",
        "f_search_semantic": "search: by meaning and keywords",
        "f_search_lexical": "search: by keywords", "f_search_claude": ", picked by Claude",
        "index_stale": "index outdated",
        "f_hours_dropped": "removed by opening hours {n}: {parts}",
        "f_profile_accessible": "route: step-free",
        "f_profile_lit": "route: at night — along major streets",
        "f_profile_auto": " (wheelchair)",
        "f_profile_req_accessible": "step-free route requested",
        "f_profile_req_lit": "route «at night — along major streets» requested",
        "f_warm_between": "warm: up to {v} min on foot between places",
        "f_short": "no long walks outside: up to {v} min at a time",
        "f_events_open": "events: not filtered by place categories",
        "f_short_between": "no long walks outside: up to {v} min between places",
        "short_long_leg": "The longest walk outside is {n} min: there is no closer "
                          "place in the data.",
        "f_prefs_likes": "likes: {v}", "f_prefs_avoids": "avoid: {v}",
        "f_excluded": "excluding places already shown: {n}",
        "summary_no_more": "No other suitable places nearby: the ones already shown "
                           "are not repeated.",
        "f_warm": "warm: up to {v} min outside at a time", "f_warm_frost": " (frost {t})",
        "warm_outdoor": "Few indoor places nearby — outdoor places are shown too.",
        "warm_long_leg": "The longest walk outside is {n} min: there is no warm place "
                         "closer in the data, dress warmly.",
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


# T123 / T133: prefs labels in the "data used" panel (ru = schemas.PREF_LABELS_RU, 1.3.1)
PREF_NAMES: dict[str, dict[str, str]] = {
    "ru": dict(PREF_LABELS_RU),
    "kk": {"quiet": "тыныш", "coffee": "кофе", "kids": "баламен", "museums": "мұражайлар",
           "parks": "саябақтар", "warm": "жылыда", "night": "түнгі",
           "bars": "барлар", "noisy": "шулы", "long_outdoor": "далада ұзақ",
           "kids_places": "балалар орындары", "cafes": "кафелер"},
    "en": {"quiet": "quiet", "coffee": "coffee", "kids": "with kids", "museums": "museums",
           "parks": "parks", "warm": "warm", "night": "night",
           "bars": "bars", "noisy": "noisy", "long_outdoor": "long walks outside",
           "kids_places": "kids' places", "cafes": "cafes"},
}


# Why the LLM / Gemini did not help (app.llm.last_error / last_embed_status), in the answer
# language: the warnings say it instead of a bare "unavailable".
LLM_REASONS: dict[str, dict[str, str]] = {
    "ru": {"disabled": "отключено в настройках", "no_key": "нет ключа API",
           "no_credit": "закончились кредиты API", "auth": "ключ не принят",
           "bad_model": "модель недоступна", "quota": "лимит запросов", "timeout": "таймаут",
           "network": "нет связи", "server": "сбой сервиса", "refusal": "модель отказалась",
           "invalid_output": "некорректный ответ модели", "bad_request": "ошибка запроса",
           "bug": "внутренняя ошибка"},
    "kk": {"disabled": "баптауларда өшірілген", "no_key": "API кілті жоқ",
           "no_credit": "API кредиттері таусылды", "auth": "кілт қабылданбады",
           "bad_model": "модель қолжетімсіз", "quota": "сұраулар лимиті",
           "timeout": "күту уақыты өтті", "network": "байланыс жоқ", "server": "қызмет ақауы",
           "refusal": "модель бас тартты", "invalid_output": "модельдің жауабы дұрыс емес",
           "bad_request": "сұрау қатесі", "bug": "ішкі қате"},
    "en": {"disabled": "disabled in settings", "no_key": "no API key",
           "no_credit": "API credits ran out", "auth": "key rejected",
           "bad_model": "model unavailable", "quota": "rate limit", "timeout": "timeout",
           "network": "no connection", "server": "service error", "refusal": "model refused",
           "invalid_output": "invalid model output", "bad_request": "request error",
           "bug": "internal error"},
}
# Reasons in brackets are for failures only. An empty embeddings key (OPENROUTER_API_KEY /
# GEMINI_API_KEY, by EMBED_PROVIDER) is a deliberate choice
# (video takes run with it), so "no_key" has no text: plain «поиск: по словам», no warning.
# {p} = the embeddings provider as the user would name it: Gemini or OpenRouter.
EMBED_REASONS: dict[str, dict[str, str]] = {
    "ru": {"quota": "лимит запросов {p}", "minute_quota": "минутный лимит {p}",
           "daily_quota": "дневная квота {p}", "network": "нет связи с {p}",
           "error": "ошибка {p}", "bad_vector": "некорректный вектор запроса",
           "auth": "ключ {p} недействителен", "no_credit": "нет кредитов {p}",
           "index_mismatch": "индекс не подходит к модели запросов",
           "index_missing": "индекс не загружен"},
    "kk": {"quota": "{p} сұраулар лимиті", "minute_quota": "{p} минуттық лимиті",
           "daily_quota": "{p} күндік квотасы", "network": "{p}-мен байланыс жоқ",
           "error": "{p} қатесі", "bad_vector": "сұрау векторы дұрыс емес",
           "auth": "{p} кілті жарамсыз", "no_credit": "{p} кредиттері жоқ",
           "index_mismatch": "индекс сұрау моделіне сәйкес емес",
           "index_missing": "индекс жүктелмеген"},
    "en": {"quota": "{p} rate limit", "minute_quota": "{p} per-minute limit",
           "daily_quota": "{p} daily quota", "network": "no connection to {p}",
           "error": "{p} error", "bad_vector": "invalid query vector",
           "auth": "{p} key rejected", "no_credit": "no {p} credits",
           "index_mismatch": "index does not match the query model",
           "index_missing": "index not loaded"},
}
EMBED_PROVIDER_NAMES = {"gemini": "Gemini", "openrouter": "OpenRouter"}
# retrieval.hours_dropped() reasons, in this order, for the "data used" panel
HOURS_DROP_NAMES: dict[str, dict[str, str]] = {
    "ru": {"closed": "закрыто", "closed_partial": "сегодня не работает",
           "closing_soon": "скоро закроется", "night": "ночью не работает"},
    "kk": {"closed": "жабық", "closed_partial": "бүгін жұмыс істемейді",
           "closing_soon": "жақында жабылады", "night": "түнде жұмыс істемейді"},
    "en": {"closed": "closed", "closed_partial": "not open today",
           "closing_soon": "closing soon", "night": "not open at night"},
}
SEMANTIC_DOWN_KINDS = frozenset({"quota", "minute_quota", "daily_quota", "network", "error",
                                 "bad_vector", "auth", "no_credit"})


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
    # The client is asked even while it cools down / has no key: its disk cache may answer
    # (preset scenarios), otherwise it fails fast. Only an explicit switch-off skips it.
    llm = deps.llm if (req.use_llm and deps.llm is not None and deps.llm.enabled) else None
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
    parse_reason = (llm.last_error or "bug") if (llm is not None
                                                  and intent.parser == "rules") else None
    intent = apply_overrides(intent, req.overrides)
    t = TEXTS[intent.lang]
    llm_down_said = False
    if req.use_llm and llm is None:
        warn.append(_llm_down(t, intent.lang, "disabled" if deps.llm is not None else None))
        llm_down_said = True
    if intent.landmark_mention and intent.landmark_mention != landmark_id:
        lm = store.landmarks.get(intent.landmark_mention)
        if lm is not None:
            name = lm.names.get(intent.lang) or next(iter(lm.names.values()), lm.id)
            warn.append(t["landmark_mismatch"].format(name=name))

    # Modes. `plan_intent` is what the search actually uses; `intent` stays what was asked.
    at_airport = landmark_id == AIRPORT_ID
    at_hub = landmark_id is not None and store.landmarks[landmark_id].kind == "transport"
    departure = req.overrides.departure_in_min if req.overrides else None
    flight = intent.tourist and not at_airport and (
        departure is not None or mentions_flight(req.query))
    # The budget is "time to departure" only when said so («2 часа перед вылетом») or sent by
    # the UI; «прогулка на 30 минут, вечером рейс» is a 30-minute walk plus the general tip.
    to_departure = flight and (departure is not None or departure_phrase(req.query))
    walk_budget: int | None = None  # minutes left for the walk before leaving for the airport
    plan_update: dict[str, object] = {}
    # T123: an explicit «избегать» (prefs.avoids, e.g. the «Без кафе» chip) beats what the
    # parser read in the text («тихое место с кофе» -> cafe): its categories and tags leave
    # the search; categories the UI set explicitly (overrides.categories) stay
    plan_update.update(_without_avoided(intent, req))
    if to_departure and intent.time_budget_min:
        walk_budget = intent.time_budget_min - AIRPORT_TRANSFER_MIN - AIRPORT_RESERVE_MIN
        plan_update["time_budget_min"] = max(MIN_WALK_BUDGET_MIN, walk_budget)
    short_walks = at_hub or intent.tourist
    if short_walks:
        plan_update["max_walk_min"] = min(intent.max_walk_min or HUB_MAX_WALK_MIN,
                                          HUB_MAX_WALK_MIN)
    # Night by the clock (22:00–06:00), unless the UI switched night mode off explicitly:
    # retrieval then keeps late_open / 24_7 / open-now places only.
    night_switch = req.overrides.night if req.overrides else None
    night_by_clock = (not intent.night and night_switch is None
                      and (local_time.hour >= NIGHT_FROM_H or local_time.hour < NIGHT_TO_H))
    if night_by_clock:
        plan_update["night"] = True
    careful = intent.with_kids or intent.wheelchair or intent.night or night_by_clock
    careful_cap = careful and intent.max_walk_min is None and "max_walk_min" not in plan_update
    if careful_cap:
        plan_update["max_walk_min"] = CAREFUL_MAX_WALK_MIN
    # «Nearby» is a walk of at most MAX_ONE_WAY_WALK_MIN to a stop: the budget radius
    # (ONE_WAY_SHARE × budget = 21 min for 60, 42 min for 120) must not go further. An explicit
    # limit from the text or the UI always wins.
    plan_budget = plan_update.get("time_budget_min", intent.time_budget_min)
    if ("max_walk_min" not in plan_update and intent.max_walk_min is None and plan_budget
            and geo.ONE_WAY_SHARE * float(plan_budget) > MAX_ONE_WAY_WALK_MIN):
        plan_update["max_walk_min"] = MAX_ONE_WAY_WALK_MIN
    # T123 «в тепле»: indoor stops close to the start and to each other (see WARM_MAX_LEG_MIN)
    _clear_weather_status()
    wx = weather.get_weather(origin, local_time)
    weather_down = _weather_status() == "unavailable"  # T133: asked, no answer in time
    warm = warm_reason(req, wx)
    # T133: avoids long_outdoor («долго на улице») — the same short walks outside as «в тепле»,
    # but without the indoor requirement
    short_out = warm is not None or (req.prefs is not None
                                     and "long_outdoor" in req.prefs.avoids)
    warm_indoor = False  # the indoor filter was set by warm mode (not by the person)
    if short_out and intent.max_walk_min is None:  # an explicit limit is kept as asked
        cap = plan_update.get("max_walk_min")
        plan_update["max_walk_min"] = min(int(cap or WARM_MAX_LEG_MIN), WARM_MAX_LEG_MIN)
    if warm is not None:
        tags = plan_update.get("must_tags", intent.must_tags)
        plan_update["must_tags"] = [x for x in tags if x not in WARM_DROP_TAGS]  # type: ignore[union-attr]
        if intent.indoor == "any":
            plan_update["indoor"], warm_indoor = "indoor", True
    plan_intent = intent.model_copy(update=plan_update) if plan_update else intent
    no_time = walk_budget is not None and walk_budget < MIN_WALK_LEFT_MIN

    # 3. context (the area label is in the answer language: the LLM and the panel read it)
    lm_here = store.landmarks.get(landmark_id) if landmark_id else None
    area_label = _localized_area(intent.lang, lm_here, h3_cell, area_label)
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

    # T123: events current at the request time (data/events.json; none / broken file -> [])
    now_events = events.current(_load_events(deps.settings), local_time)
    by_event = {e.id: e for e in now_events}
    event_pool = [(events.as_poi(e, intent.lang), events.verdict(e, local_time, intent.lang))
                  for e in now_events]

    # 4. query vector (Gemini, optional; never in the no-AI mode)
    query_vec = None
    semantic_reason: str | None = None  # why the search is lexical although an index exists
    if req.use_llm and deps.llm is not None and store.embeddings is not None:
        raw_vec = deps.llm.embed_query(intent.raw_query)
        query_vec = _usable_vector(raw_vec, store.embeddings)
        if query_vec is None:
            status = getattr(deps.llm, "last_embed_status", None)
            semantic_reason = "bad_vector" if raw_vec is not None else (status or "error")
            if semantic_reason in SEMANTIC_DOWN_KINDS:
                warn.append(t["semantic_down"].format(
                    reason=_embed_reason(intent.lang, semantic_reason, deps.settings)))
    elif req.use_llm and deps.llm is not None:
        # the provider has a key but Store dropped the index (another model / dim, broken,
        # missing): say so instead of a silent «по словам»
        semantic_reason = index_problem(deps.settings, store)
        if semantic_reason is not None:
            warn.append(t[f"semantic_{semantic_reason}"])

    wide_pool = llm is not None and llm.available and query_vec is None
    pool_k = LLM_POOL_K if wide_pool else TOP_K

    # T123: prefs re-rank and filter the search (retrieval); labels only, never logged.
    # Warm mode adds "warm" to the likes: warm stops first, whatever switched it on.
    search_prefs = _search_prefs(req.prefs, warm=warm is not None)
    # «Ещё»: the places and events already shown in this series are never candidates again
    # (ids only, per request, never logged)
    exclude = frozenset(req.exclude_poi_ids)
    # Events (T133, Megah): only an explicit category — named in the text («хочу в музей»)
    # or set in the UI — filters them; general words («что интересного», «куда сходить»)
    # widen the place categories but do not hide events. «концерт или шоу?»: the parsed tags
    # (live_music) do not filter or penalise events either.
    events_asked = bool(event_pool) and retrieval.asks_for_events(intent)
    search_kw: dict[str, object] = {"prefs": search_prefs, "event_pool": event_pool,
                                    "exclude": exclude, "events_asked": events_asked,
                                    "event_categories": _event_categories(req, intent)}

    # 5. retrieve: as asked -> radius x1.5 once -> without the category filter (sparse areas,
    #    e.g. 3 cafes and no sights around the airport). `search_intent` is what was applied.
    search_intent = plan_intent
    if no_time:  # nothing fits before the flight: do not search, do not spend the LLM
        candidates, failed = [], False
    else:
        candidates, failed = _retrieve(store, search_intent, ctx, query_vec, pool_k, **search_kw)
    if short_walks:  # "two cards beat a third one 48 minutes away"
        candidates = [c for c in candidates if c.walk_min <= HUB_MAX_WALK_MIN]
    if not candidates and not failed and not no_time and not short_walks:
        wider = min(round(ctx.radius_m * WIDEN_FACTOR), MAX_RADIUS_M)
        if wider > ctx.radius_m:
            ctx = ctx.model_copy(update={"radius_m": wider})
            candidates, failed = _retrieve(store, search_intent, ctx, query_vec, pool_k,
                                           **search_kw)
            if candidates:
                warn.append(t["widened"].format(r=wider))
    if plan_intent.night and not no_time:
        # At night "surely open" beats "close": keep open / late_open / 24_7 places; if none
        # is within the short-walk cap, look further (the taxi back is advised anyway).
        sure = [c for c in candidates if _night_sure(c, by_event, local_time)]
        if not sure and careful_cap and not short_walks and not failed:
            far_intent = plan_intent.model_copy(update={"max_walk_min": intent.max_walk_min})
            far_ctx = ctx.model_copy(update={"radius_m": min(
                round(_safe_radius(far_intent) * WIDEN_FACTOR), MAX_RADIUS_M)})
            more, failed = _retrieve(store, far_intent, far_ctx, query_vec, pool_k, **search_kw)
            sure = [c for c in more if _night_sure(c, by_event, local_time)]
            if sure:
                search_intent, ctx = far_intent, far_ctx
                warn.append(t["night_far"])
        if sure:
            candidates = sure
    if not candidates and not failed and not no_time and plan_intent.categories:
        search_intent = plan_intent.model_copy(update={"categories": []})
        candidates, failed = _retrieve(store, search_intent, ctx, query_vec, pool_k, **search_kw)
        if short_walks:
            candidates = [c for c in candidates if c.walk_min <= HUB_MAX_WALK_MIN]
        if plan_intent.night:
            candidates = [c for c in candidates
                          if _night_sure(c, by_event, local_time)] or candidates
        if candidates:
            warn.append(t["relaxed"])
    if warm_indoor and len(candidates) < WARM_MIN_INDOOR and not failed and not no_time:
        # too few indoor places around: outdoor ones come back, and the person is told
        wider_intent = search_intent.model_copy(update={"indoor": "any"})
        more, failed = _retrieve(store, wider_intent, ctx, query_vec, pool_k, **search_kw)
        if short_walks:
            more = [c for c in more if c.walk_min <= HUB_MAX_WALK_MIN]
        if plan_intent.night:
            more = [c for c in more if _night_sure(c, by_event, local_time)] or more
        if len(more) > len(candidates):
            search_intent, candidates = wider_intent, more
            warn.append(t["warm_outdoor"])
    candidates = _without_event_venues(candidates, by_event)
    dropped = {} if no_time else _hours_dropped(store, search_intent, ctx, search_prefs)
    # warm mode drops the walk tags on purpose: «погулять» then is not an unmatched query
    asked_intent = (search_intent if warm is None
                    else search_intent.model_copy(update={"must_tags": intent.must_tags}))
    if candidates and _query_unmatched(store, asked_intent, ctx, candidates, search_prefs):
        warn.append(t["no_match"])
    if 0 < len(candidates) < SPARSE_MIN:
        warn.append(t["sparse"].format(n=len(candidates)))

    # T91: the walk graph (loaded once, cached); None -> straight segments as before
    graph = _walk_graph(deps.settings, request_id)
    # T123: the route profile goes to plan_route / plan_walk as an argument (with and without
    # the graph) and into the Mode of the card minutes, so cards and route agree
    profile, profile_auto = route_profile(req.route_profile, intent)
    walk_mode = walkgraph.Mode(wheelchair=intent.wheelchair, night=plan_intent.night,
                               kids=intent.with_kids, profile=profile)

    # T71: a walk of N minutes -> pick stops that fit together as a loop, then write them up
    walk_loop = (bool(plan_intent.time_budget_min) and "walk" in plan_intent.must_tags
                 and not intent.tourist and not plan_intent.night and not short_out)
    gen_candidates = candidates
    if walk_loop and req.want_route and len(candidates) >= 2:
        try:
            budget, wanted_cats = plan_intent.time_budget_min or 0, set(plan_intent.categories)
            if graph is None:
                picked = route.plan_walk(ctx.origin, candidates, budget,
                                         wanted_categories=wanted_cats, profile=profile)
            else:
                picked = route.plan_walk(ctx.origin, candidates, budget,
                                         wanted_categories=wanted_cats, graph=graph,
                                         mode=walk_mode, origin_landmark=ctx.landmark_id,
                                         profile=profile)
        except Exception as exc:  # noqa: BLE001 - the cards are still useful without it
            log.error("request %s: plan_walk failed (%s)\n%s", request_id, type(exc).__name__,
                      _frames(exc))
            warn.append(t["route_failed"])
            picked = []
        if len(picked) >= 2:
            gen_candidates = picked
        elif picked:  # one stop fits: it goes first, the rest stay as alternatives
            wanted = set(plan_intent.categories)
            gen_candidates = picked + [c for c in candidates if c not in picked
                                       and route.walk_eligible(c, wanted)]

    # T91: distance and minutes on the cards of the places that may be shown come from the
    # graph too, both of them (ranking is untouched: it happened with the straight estimate);
    # the walking limit (max_walk_min, transport hub) is then checked with the graph minutes
    if graph is not None and gen_candidates:
        gen_candidates = _graph_minutes(graph, ctx, gen_candidates, search_intent, walk_mode,
                                        request_id)
        within = [c for c in gen_candidates
                  if not plan_intent.max_walk_min or c.walk_min <= plan_intent.max_walk_min]
        if within:
            gen_candidates = within
        else:
            warn.append(t["graph_over_limit"].format(v=plan_intent.max_walk_min))
    if short_out:  # short walks outside between the stops too
        gen_candidates = warm_cluster(gen_candidates, indoor_first=warm is not None)

    # 6. cards (event candidates become kind="event" cards: validate gets the events)
    ev_kw: dict[str, object] = {"events": by_event} if by_event else {}
    cards: list[Card] = []
    generation = "template"
    summary: str | None = None
    gen_model: str | None = None
    llm_summary: str | None = None
    if candidates and llm is not None:
        draft = None
        try:
            draft = draft_cards(search_intent, ctx, gen_candidates, llm, prefs=search_prefs,
                                **ev_kw)
        except LLMUnavailable:
            reason = llm.last_error or "bug"
            log.info("request %s: generation fell back to templates (%s)", request_id, reason)
            warn.append(_llm_down(t, intent.lang, reason))
            llm_down_said = True
        if draft is not None:
            try:
                cards, fin_warnings = validate.finalize(draft, gen_candidates, search_intent,
                                                        ctx, **ev_kw)
                warn.extend(fin_warnings)
                draft_ids = {d.poi_id for d in draft.cards}
                if any(c.poi_id in draft_ids for c in cards):
                    generation, summary, gen_model = "llm", draft.summary, llm.last_model
                    llm_summary = draft.summary
            except Exception as exc:  # noqa: BLE001 - a finalize bug must not kill the answer
                log.error("request %s: finalize failed (%s)\n%s", request_id,
                          type(exc).__name__, _frames(exc))
                warn.append(t["gen_check_failed"])
                cards = []
    if generation == "llm":
        cards = _untag_whys(cards, gen_candidates, search_intent, ctx, request_id, ev_kw)
    if parse_reason is not None and not llm_down_said:
        warn.append(t["parse_fallback"].format(
            reason=LLM_REASONS[intent.lang].get(parse_reason, LLM_REASONS[intent.lang]["bug"])))
    if candidates and not cards:
        try:
            cards = validate.template_cards(gen_candidates[:TOP_K], search_intent, ctx,
                                            **ev_kw)
        except Exception as exc:  # noqa: BLE001
            log.error("request %s: template_cards failed (%s)\n%s", request_id,
                      type(exc).__name__, _frames(exc))
            failed = True
    cards = cards[: TOURIST_MAX_CARDS if walk_budget is not None else 3]
    cards = _localize_hours(cards, candidates, local_time, intent.lang)

    # 7. route
    note = route.route_note(intent.lang, loop=walk_loop, kids=intent.with_kids,
                            wheelchair=intent.wheelchair)
    graph_note = route.route_note(intent.lang, loop=walk_loop, kids=intent.with_kids,
                                  wheelchair=intent.wheelchair, graph=True)
    # At a transport hub people pick one place to wait: a tour of three cafes makes no sense.
    # At night with every place over the walking limit (E2: 02:30, 29 and 33 min away) a long
    # walking route is bad advice: no route, the cards stay, the taxi is advised once.
    night_taxi = _night_taxi(plan_intent, cards)
    if night_taxi:
        over = t["graph_over_limit"].format(v=plan_intent.max_walk_min)
        warn[:] = [t["night_far_open"] if w == t["night_far"] else w
                   for w in warn if w != over]
    mini_route, route_broke = _route(ctx, cards, candidates, search_intent,
                                     req.want_route and not at_hub and not night_taxi,
                                     request_id, loop=walk_loop, note=note, graph=graph,
                                     mode=walk_mode, graph_note=graph_note, profile=profile)
    if route_broke:
        warn.append(t["route_failed"])
    if (short_out and mini_route is not None and mini_route.max_outdoor_leg_min
            and mini_route.max_outdoor_leg_min > WARM_MAX_LEG_MIN):
        key = "warm_long_leg" if warm is not None else "short_long_leg"
        warn.append(t[key].format(n=mini_route.max_outdoor_leg_min))

    # 8. warnings, data used, answer
    if failed and not cards:
        warn.append(t["search_error"])
    elif not cards and not no_time:
        warn.append(t["nothing"])
    if walk_budget is not None:
        # first: the deadline matters more than any other warning (POLISH request)
        warn.insert(0, _airport_warning(t, intent.time_budget_min or 0, walk_budget, local_time))
    warn.extend(_context_warnings(t, plan_intent, cards, candidates, mini_route, at_hub=at_hub,
                                  flight=flight, deadline_given=walk_budget is not None,
                                  night_taxi=night_taxi))
    if summary is None:
        summary = _template_summary(t, len(cards))
    if not cards and exclude and not candidates and not failed and not no_time:
        summary = t["summary_no_more"]  # an honestly empty pick, not a failed search
    if no_time:
        summary = t["summary_leave_now"]
    elif at_airport and intent.tourist and _sights(candidates) < 2 and not _food_only(intent):
        # E11 / UI "tourist + departure": NQZ has a few cafes and nothing to see — say so,
        # whatever categories were asked (the UI sends no text categories at all)
        summary = _airport_summary(t, intent, has_cards=bool(cards))
    by_llm = generation == "llm" and summary == llm_summary
    if by_llm and _tag_leak(summary, intent.lang, [c.title for c in cards]):
        # S5 live: «…все с тегом night…» — an internal id is no text for a person
        log.info("request %s: model summary names a raw tag: template summary", request_id)
        summary, by_llm = _template_summary(t, len(cards)), False
    # "open now" never leaks via the summary; model text is checked as model text
    summary = _sanitize_summary(summary, cards, by_llm=by_llm, now=local_time)
    if by_llm and not summary.strip():  # its only sentence was cut: the template one instead
        summary = _template_summary(t, len(cards))

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
            weather=_weather_label(t, wx, intent.lang, local_time, manual=req.at is not None,
                                   down=weather_down),
            filters=_filters(t, search_intent, ctx, req.use_llm, lang=intent.lang,
                             semantic=query_vec is not None,
                             claude_picked=wide_pool and generation == "llm",
                             semantic_reason=_embed_reason(intent.lang, semantic_reason,
                                                           deps.settings),
                             index_stale=index_status(deps.settings) == "stale",
                             hours_dropped=dropped,
                             night_by_clock=night_by_clock,
                             airport=(intent.time_budget_min or 0) if walk_budget is not None
                             else None,
                             profile=profile, profile_auto=profile_auto,
                             profile_applied=_profile_applied(mini_route, profile),
                             prefs=req.prefs, warm=warm, short_out=short_out,
                             events_open=bool(search_intent.categories) and any(
                                 c.kind == "event" and c.category not in search_intent.categories
                                 for c in cards),
                             excluded=len(exclude),
                             temp_c=wx.temp_c if wx is not None else None),
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


def _embed_reason(lang: Lang, kind: str | None, settings: Settings) -> str | None:
    """«дневная квота Gemini» / «ключ OpenRouter недействителен»; None for no reason / no key."""
    template = EMBED_REASONS[lang].get(kind or "")
    if template is None:
        return None
    return template.format(p=EMBED_PROVIDER_NAMES.get(settings.embed_provider, "Gemini"))


def _frames(exc: BaseException) -> str:
    """Last stack frames only: an exception message may echo the user's query."""
    return "".join(traceback.format_tb(exc.__traceback__)[-4:])


def _llm_down(t: dict[str, str], lang: Lang, reason: str | None) -> str:
    text = LLM_REASONS[lang].get(reason or "")
    return t["llm_down_r"].format(reason=text) if text else t["llm_down"]


def _weather_label(t: dict[str, str], wx: object, lang: Lang, local_time: datetime, *,
                   manual: bool, down: bool = False) -> str:
    if wx is not None:
        return weather.summary_for(wx, lang)  # type: ignore[arg-type]
    if manual and weather.is_time_machine(local_time):
        return t["weather_manual"]  # not asked on purpose, not a failure
    if down:  # Open-Meteo did not answer within weather.TIMEOUT_S: the answer goes on without it
        return t["weather_no_answer"]
    return t["weather_unknown"]


def _clear_weather_status() -> None:
    fn = getattr(weather, "clear_status", None)
    if fn is not None:
        fn()


def _weather_status() -> str | None:
    fn = getattr(weather, "last_status", None)
    return fn() if fn is not None else None


def _usable_vector(vec: np.ndarray | None, index: np.ndarray | None) -> np.ndarray | None:
    """The query vector only if retrieval will really use it (same checks as
    retrieval._semantic_scores); otherwise None, so the panel honestly says «по словам»."""
    if vec is None or index is None or index.ndim != 2:
        return None
    q = np.asarray(vec, dtype=np.float32).ravel()
    if q.shape[0] != index.shape[1]:
        log.warning("query vector dim %d does not match the index: lexical search", q.shape[0])
        return None
    norm = float(np.linalg.norm(q))
    if not np.isfinite(norm) or norm == 0.0:
        log.warning("query vector is zero or not finite: lexical search")
        return None
    return q


_META_CACHE: dict[str, tuple[tuple[int, int], dict[str, object] | None]] = {}


def index_meta(settings: Settings) -> dict[str, object] | None:
    """data/index/meta.json as a dict (None: no file / unreadable), re-read when the file
    changes. The structured source: Store only keeps human warning texts about it."""
    path = settings.index_dir / "meta.json"
    try:
        stat = path.stat()
    except OSError:
        return None
    stamp = (stat.st_mtime_ns, stat.st_size)
    hit = _META_CACHE.get(str(path))
    if hit is not None and hit[0] == stamp:
        return hit[1]
    try:
        meta = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        meta = None
    meta = meta if isinstance(meta, dict) else None
    _META_CACHE[str(path)] = (stamp, meta)
    return meta


def index_status(settings: Settings) -> str | None:
    """status from meta.json: "ok" | "stale" | None."""
    status = (index_meta(settings) or {}).get("status")
    return status if isinstance(status, str) else None


def index_problem(settings: Settings, store: Store) -> str | None:
    """Why the vectors are not used although the embeddings provider has a key:
    "index_mismatch" (meta.json model / dim differ from the settings, e.g. EMBED_PROVIDER=
    openrouter over a gemini-embedding-001 index) or "index_missing" (no / broken index).
    None when the index is in use or the key is deliberately empty."""
    if store.embeddings is not None or not settings.has_embeddings:
        return None
    meta = index_meta(settings) or {}
    model, dim = meta.get("model"), meta.get("dim")
    if model is not None or dim is not None:
        if str(model) != settings.embed_model or str(dim) != str(settings.embed_dim):
            return "index_mismatch"
    return "index_missing"


def _hours_dropped(store: Store, intent: QueryIntent, ctx: SearchContext,
                   prefs: UserPrefs | None = None) -> dict[str, int]:
    """retrieval.hours_dropped (QUALITY) for the panel; absent or broken -> {}."""
    fn = getattr(retrieval, "hours_dropped", None)
    if fn is None:
        return {}
    try:
        counts = fn(store, intent, ctx, prefs=prefs) if prefs else fn(store, intent, ctx)
        return {str(k): int(v) for k, v in dict(counts).items()}
    except Exception as exc:  # noqa: BLE001 - a panel line never kills the answer
        log.error("hours_dropped failed (%s)\n%s", type(exc).__name__, _frames(exc))
        return {}


def _query_unmatched(store: Store, intent: QueryIntent, ctx: SearchContext,
                     candidates: list[Candidate], prefs: UserPrefs | None = None) -> bool:
    """QUALITY decides (retrieval.query_unmatched): the query has meaningful words and none of
    them matches any candidate, so the cards are just what is nearby. Absent (older main) or
    broken -> no warning, never a failed answer."""
    check = getattr(retrieval, "query_unmatched", None)
    if check is None:
        return False
    try:
        if prefs is not None:
            return bool(check(store, intent, ctx, candidates, prefs=prefs))
        return bool(check(store, intent, ctx, candidates))
    except Exception as exc:  # noqa: BLE001 - a missing hint never kills the answer
        log.error("query_unmatched failed (%s)\n%s", type(exc).__name__, _frames(exc))
        return False


# Raw TAG_VOCAB ids must not reach a person. Ids with "_" / digits are never words (any
# language); a bare id (night, kids, wifi) is a tag in ru / kk text, in en only next to
# "tag(s/ged)" («a quiet night walk» is English). Card titles are skipped: «Kids Park».
_ID_ALT = "|".join(re.escape(x) for x in sorted(TAG_VOCAB, key=len, reverse=True))
_STRUCT_ALT = "|".join(re.escape(x) for x in sorted(TAG_VOCAB)
                       if "_" in x or any(ch.isdigit() for ch in x))
_STRUCT_TAG_RE = re.compile(rf"(?<![\w-])(?:{_STRUCT_ALT})(?![\w-])")
_BARE_TAG_RE = re.compile(rf"(?<![\w-])(?:{_ID_ALT})(?![\w-])")
_TAG_WORD_RE = re.compile(r"(?<!\w)(?:тег(?:а|у|ом|е|и|ов|ам|ами|ах|і|тер|тері)?)(?!\w)",
                          re.IGNORECASE)
_EN_TAG_NEAR_RE = re.compile(
    rf"\btag(?:s|ged)?\W+(?:\w+\W+){{0,2}}?(?:{_ID_ALT})\b"
    rf"|\b(?:{_ID_ALT})\W+(?:\w+\W+){{0,2}}?tag(?:s|ged)?\b", re.IGNORECASE)


def _tag_leak(text: str, lang: str, titles: Iterable[str]) -> bool:
    for title in titles:
        if title.strip():
            text = re.sub(re.escape(title), " ", text, flags=re.IGNORECASE)
    if _STRUCT_TAG_RE.search(text) or _TAG_WORD_RE.search(text):
        return True
    if lang == "en":
        return _EN_TAG_NEAR_RE.search(text) is not None
    return _BARE_TAG_RE.search(text) is not None


def _untag_whys(cards: list[Card], candidates: list[Candidate], intent: QueryIntent,
                ctx: SearchContext, request_id: str,
                ev_kw: dict[str, object] | None = None) -> list[Card]:
    """A model-written why naming a raw tag («есть кофе и wifi», «отмечена тегом …») gets the
    template why of the same candidate (validate.template_cards); nothing else changes."""
    by_id = {c.poi.id: c for c in candidates}
    out: list[Card] = []
    for card in cards:
        cand = by_id.get(card.poi_id)
        if cand is not None and _tag_leak(card.why, intent.lang, [card.title]):
            try:
                why = validate.template_cards([cand], intent, ctx, n=1, **(ev_kw or {}))[0].why
            except Exception as exc:  # noqa: BLE001 - keep the card as it is
                log.error("request %s: template why for card %d failed (%s)\n%s", request_id,
                          card.rank, type(exc).__name__, _frames(exc))
                why = ""
            if why.strip():
                log.info("request %s: card %d why names a raw tag: template why", request_id,
                         card.rank)
                card = card.model_copy(update={"why": why})
        out.append(card)
    return out


def _sanitize_summary(summary: str, cards: list[Card], *, by_llm: bool,
                      now: datetime | None = None) -> str:
    """validate.sanitize_summary(summary, cards, llm=..., now=...) with what the version takes:
    `llm` (stricter for model text), `now` (T123: «идёт сейчас» about events)."""
    fn = validate.sanitize_summary
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        params = {}  # type: ignore[assignment]
    kw: dict[str, object] = {}
    if "llm" in params:
        kw["llm"] = by_llm
    if "now" in params:
        kw["now"] = now
    return fn(summary, cards, **kw)


def _sights(candidates: list[Candidate]) -> int:
    return sum(1 for c in candidates if c.poi.category in SIGHT_CATEGORIES)


def _food_only(intent: QueryIntent) -> bool:
    """The person asked for food / coffee only: «nothing to see here» would be off-topic."""
    return bool(intent.categories) and set(intent.categories) <= FOOD_CATEGORIES


def _night_sure(c: Candidate, by_event: dict[str, events.Event] | None = None,
                now: datetime | None = None) -> bool:
    """Open now by the data, or marked as working late / round the clock; an event: a session
    on now or starting within EVENT_NIGHT_SOON_MIN (a run of many days never)."""
    event = (by_event or {}).get(c.poi.id)
    if event is not None and now is not None:
        if not events.is_session(event):  # a run (exhibition): its night hours are unknown
            return False
        soon = timedelta(0) <= event.starts_at - now <= timedelta(minutes=EVENT_NIGHT_SOON_MIN)
        return events.is_live(event, now) or soon
    return c.hours.status == "open" or bool({"late_open", "24_7"} & set(c.poi.tags))


def _load_events(settings: Settings) -> list[events.Event]:
    """settings.events_path (QADAM_EVENTS_PATH, default data/events.json; eval points it at
    its own fixture); None, no file or any problem -> no events, never a failed answer."""
    path = getattr(settings, "events_path", None)
    if path is None:
        return []
    try:
        return events.load_events(path)
    except Exception as exc:  # noqa: BLE001 - events are an extra, the places still answer
        log.error("events failed to load (%s)", type(exc).__name__)
        return []


def _without_event_venues(candidates: list[Candidate], by_event: dict[str, events.Event]
                          ) -> list[Candidate]:
    """A venue whose event is among the candidates gives its place to the event: «Тоска в
    Астана Опере» says more than «Астана Опера» next to it."""
    venues = {by_event[c.poi.id].venue_poi_id for c in candidates if c.poi.id in by_event}
    venues.discard(None)
    return [c for c in candidates if c.poi.id not in venues] if venues else candidates


def _localized_area(lang: Lang, landmark: Landmark | None, cell: str, ru_label: str) -> str:
    """geo builds the Russian label («у EXPO», «ваш район (≈13 га) у …»); kk / en here.
    The cell area is in hectares, as the browser shows it: an r10 cell is ≈1,8 га, and
    «≈0,0 км²» said nothing."""
    if lang == "ru":
        return ru_label
    if landmark is not None:
        name = landmark.names.get(lang) or landmark.names.get("ru") or landmark.id
        return f"near {name}" if lang == "en" else f"{name} маңында"
    area = _format_ha(h3.cell_area(cell, unit="km^2") * 100, lang)
    if lang == "en":
        return f"your area (≈{area} ha)"
    return f"сіздің ауданыңыз (≈{area} га)"


def _format_ha(ha: float, lang: Lang) -> str:
    """Hectares like web/app.js cellAreaHa (Intl.NumberFormat, maximumFractionDigits 0 from
    10 ha, else 1): half up, no trailing «,0», groups of thousands; kk «1,8», en «1.8»."""
    step = Decimal(1) if ha >= 10 else Decimal("0.1")
    value = Decimal(repr(ha)).quantize(step, rounding=ROUND_HALF_UP)
    if value == value.to_integral_value():
        value = value.quantize(Decimal(1))
    text = f"{value:,}"  # «3,612.5»
    if lang == "en":
        return text
    return text.replace(",", "\u00a0").replace(".", ",")  # 3 612,5 (no-break space)


def _localize_hours(cards: list[Card], candidates: list[Candidate], at: datetime, lang: Lang
                    ) -> list[Card]:
    """Hours notes in kk / en (texts live in app.hours). Status and confidence are untouched:
    a card is only re-worded when hours.py gives the very same verdict."""
    if lang == "ru":
        return cards
    by_id = {c.poi.id: c for c in candidates}
    out: list[Card] = []
    for card in cards:
        if card.kind == "event":  # the event time, written by app.events in this language
            out.append(card)
            continue
        cand = by_id.get(card.poi_id)
        if cand is None:  # never leave a Russian note in a kk / en answer
            out.append(card.model_copy(update={"open_note": hours.check_note(lang)}))
            continue
        verdict = hours.evaluate_hours(cand.poi.opening_hours, at, lang=lang)
        if (verdict.status, verdict.confidence) != (card.open_status, card.open_confidence):
            out.append(card.model_copy(update={"open_note": hours.check_note(lang)}))
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
    store: Store, intent: QueryIntent, ctx: SearchContext, query_vec: np.ndarray | None,
    k: int = TOP_K, *, prefs: UserPrefs | None = None,
    event_pool: list[tuple[POI, HoursVerdict]] | None = None,
    exclude: frozenset[str] = frozenset(),
    events_asked: bool = False,
    event_categories: list[str] | None = None,
) -> tuple[list[Candidate], bool]:
    """(candidates, failed). Wave 8 keywords go to retrieve only when set (1.2.0 calls stay)."""
    extra: dict[str, object] = {}
    if prefs is not None:
        extra["prefs"] = prefs
    if exclude:
        extra["exclude_ids"] = exclude
        event_pool = [pair for pair in event_pool or [] if pair[0].id not in exclude]
    if event_pool:
        extra["events"] = event_pool
        if events_asked:
            extra["events_asked"] = True
        if event_categories is not None:
            extra["event_categories"] = event_categories
    try:
        return retrieval.retrieve(store, intent, ctx, query_vec, k=k, **extra), False
    except Exception as exc:  # noqa: BLE001 - an empty answer beats a 500
        # data problem (bad index / record) vs a bug in the code: both need a fix, not a retry
        kind = "data" if isinstance(exc, (ValueError, KeyError, IndexError)) else "bug"
        log.error("retrieve failed (%s, %s)\n%s", kind, type(exc).__name__, _frames(exc))
        if not event_pool:
            return [], True
    # events are an extra (hand-made data): the places must still answer without them
    extra.pop("events")
    extra.pop("events_asked", None)
    extra.pop("event_categories", None)
    try:
        return retrieval.retrieve(store, intent, ctx, query_vec, k=k, **extra), False
    except Exception as exc:  # noqa: BLE001
        log.error("retrieve without events failed (%s)\n%s", type(exc).__name__, _frames(exc))
        return [], True


def _without_avoided(intent: QueryIntent, req: RecommendRequest) -> dict[str, object]:
    """plan_intent updates: the parsed categories / must_tags the person avoids, removed."""
    cats, tags = retrieval.avoided(req.prefs)
    explicit = set(req.overrides.categories or []) if req.overrides is not None else set()
    out: dict[str, object] = {}
    kept_cats = [c for c in intent.categories if c not in cats or c in explicit]
    if kept_cats != intent.categories:
        out["categories"] = kept_cats
    kept_tags = [x for x in intent.must_tags if x not in tags]
    if kept_tags != intent.must_tags:
        out["must_tags"] = kept_tags
    return out


# an exhibition is filed as a museum or a gallery: for events one names the other too
EVENT_CATEGORY_KIN: dict[str, tuple[str, ...]] = {"museum": ("gallery",), "gallery": ("museum",)}


def _event_categories(req: RecommendRequest, intent: QueryIntent) -> list[str]:
    """The categories events must have: set in the UI, else named in the text (not the
    general «что интересного»), minus what the person avoids; [] = any."""
    if req.overrides is not None and req.overrides.categories:
        return list(req.overrides.categories)
    avoided, _ = retrieval.avoided(req.prefs)
    named = [c for c in explicit_categories(req.query, intent.lang) if c not in avoided]
    return list(dict.fromkeys([*named, *(k for c in named for k in EVENT_CATEGORY_KIN.get(c, ()))]))


def warm_reason(req: RecommendRequest, wx: WeatherInfo | None) -> str | None:
    """Why «в тепле» is on: "like" (prefs likes warm), "indoor" (the UI switch indoor_only),
    "frost" (air temperature ≤ FROST_C by Open-Meteo), or None. Text like «в тепле» only
    sets the indoor filter (query_parser), as before."""
    prefs = req.prefs
    if prefs is not None and "warm" in prefs.likes:
        return "like"
    if req.overrides is not None and req.overrides.indoor_only:
        return "indoor"
    if wx is not None and wx.temp_c <= FROST_C:
        return "frost"
    return None


def _search_prefs(prefs: UserPrefs | None, *, warm: bool) -> UserPrefs | None:
    """The prefs retrieval applies: the person's labels, plus "warm" in warm mode."""
    likes, avoids = (list(prefs.likes), list(prefs.avoids)) if prefs is not None else ([], [])
    if warm and "warm" not in likes:
        likes.append("warm")
    if not likes and not avoids:
        return None
    return UserPrefs(likes=likes, avoids=avoids)  # type: ignore[arg-type]


def warm_cluster(candidates: list[Candidate], *, indoor_first: bool = True
                 ) -> list[Candidate]:
    """Best first, then every candidate within WARM_MAX_LEG_MIN on foot (straight × 1,3, as
    on the cards) of all the kept ones: any 2–3 of them make a route with short walks outside.
    `indoor_first` («в тепле»): only indoor places when there are enough of them."""
    indoor = [c for c in candidates if retrieval.is_indoor(c.poi) is True]
    pool = indoor if indoor_first and len(indoor) >= WARM_MIN_INDOOR else candidates
    kept: list[Candidate] = []
    for cand in pool:
        if all(geo.walk_minutes(geo.haversine_m(cand.poi.geo, k.poi.geo)) <= WARM_MAX_LEG_MIN
               for k in kept):
            kept.append(cand)
    return kept


def _temp(value: float | None) -> str:
    """«−20 °C» with a real minus sign."""
    return "" if value is None else f"{round(value):+d} °C".replace("-", "\u2212")


def _profile_applied(mini_route: MiniRoute | None, profile: RouteProfile) -> bool:
    """The route really follows the profile: on the graph and planned with it; «без ступенек»
    (contract 1.3.1) only with no stairs and straight_share == 0 (no straight segment: their
    steps are not known; None = not counted = not claimed). Otherwise «запрошен …»."""
    if mini_route is None or mini_route.path_source != "graph" or mini_route.profile != profile:
        return False
    if profile == "accessible":
        return mini_route.steps_count == 0 and mini_route.straight_share == 0
    return True


def route_profile(asked: RouteProfile, intent: QueryIntent) -> tuple[RouteProfile, bool]:
    """(profile to plan with, switched on by the wheelchair?). A wheelchair (the UI switch or
    the text) wants a step-free way: the default "fastest" becomes "accessible", and the
    filters panel says why. An explicit "lit" stays: Mode.wheelchair forbids stairs there too."""
    if intent.wheelchair and asked == "fastest":
        return "accessible", True
    return asked, False


def _night_taxi(intent: QueryIntent, cards: list[Card]) -> bool:
    """Night and every card over the walking limit (graph or straight minutes, as shown)."""
    limit = intent.max_walk_min
    return bool(intent.night and limit and cards and all(c.walk_min > limit for c in cards))


def _walk_graph(settings: Settings, request_id: str) -> walkgraph.WalkGraph | None:
    """The walk graph when WALK_GRAPH is on and the file is there; None on any problem."""
    if not settings.walk_graph_ready:
        return None
    try:
        return walkgraph.load_graph(settings.walk_graph_path, settings.walk_graph_meta_path)
    except Exception as exc:  # noqa: BLE001 - the straight route is always available
        log.error("request %s: walk graph failed (%s)", request_id, type(exc).__name__)
        return None


def _graph_minutes(
    graph: walkgraph.WalkGraph,
    ctx: SearchContext,
    candidates: list[Candidate],
    intent: QueryIntent,
    mode: walkgraph.Mode,
    request_id: str,
) -> list[Candidate]:
    """distance_m and walk_min of these candidates along the graph (one search from the
    start), their reasons («≈N мин пешком») rebuilt to match. A card shows both values from
    the same source: where the graph is not usable for a place (lengths_from leaves it out)
    both stay straight. Any problem -> the straight values."""
    try:
        lengths = graph.lengths_from(ctx.origin, {c.poi.id: c.poi.geo for c in candidates},
                                     mode, origin_landmark=ctx.landmark_id)
        out: list[Candidate] = []
        for cand in candidates:
            metres = lengths.get(cand.poi.id)
            if metres is None:
                out.append(cand)
                continue
            minutes = max(1, math.ceil(metres / walkgraph.WALK_M_PER_MIN - 1e-9))
            moved = cand.model_copy(update={"walk_min": minutes, "distance_m": round(metres)})
            moved.matched = retrieval.explain(moved, intent, ctx)
            out.append(moved)
        return out
    except Exception as exc:  # noqa: BLE001 - straight minutes are still right enough
        log.error("request %s: graph minutes failed (%s)", request_id, type(exc).__name__)
        return candidates


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
    graph: walkgraph.WalkGraph | None = None,
    mode: walkgraph.Mode = walkgraph.WALK,
    graph_note: str | None = None,
    profile: RouteProfile = "fastest",
) -> tuple[MiniRoute | None, bool]:
    """(route, failed): a crash in plan_route is reported, not swallowed. `profile` is passed
    with and without the graph: a straight route comes back as "fastest" and its note says the
    asked profile was not applied (route.plan_route).
    T133: an event card is a stop only if it starts within EVENT_STOP_MAX_WAIT_MIN (or is on),
    and then the last one (`last_stop_id`; the earliest such event, the others stay cards).
    It goes first in `stops`: plan_route drops the lowest-ranked stop when the budget is
    short, and a place can wait while the event will not. An event longer than the whole
    budget is no stop (its card stays). A plan_route without `last_stop_id` (older CORE) gets
    no event stop at all."""
    if not want_route or len(cards) < 2:
        return None, False
    by_id = {c.poi.id: c for c in candidates}
    visits = {c.poi_id: c.visit_min_low for c in cards}
    last = _event_stop(cards, ctx.local_time)
    if last is not None and not _takes_last_stop():
        last = None
    budget = intent.time_budget_min
    if last is not None and budget and visits.get(last, 0) >= budget:
        last = None  # the event alone fills the time: a card, not a stop
    stops = [by_id[c.poi_id] for c in cards if c.poi_id in by_id
             and (c.kind == "place" or c.poi_id == last)]
    stops.sort(key=lambda c: c.poi.id != last)  # the event first (kept longest), else rank
    if len(stops) < 2:
        return None, False
    extra: dict[str, object] = {"last_stop_id": last} if last is not None else {}
    try:
        if graph is None:
            return route.plan_route(ctx.origin, stops, intent.time_budget_min, visits,
                                    loop=loop, note=note, lang=intent.lang,
                                    profile=profile, **extra), False
        return route.plan_route(
            ctx.origin, stops, intent.time_budget_min, visits, loop=loop, note=note,
            graph=graph, mode=mode, origin_landmark=ctx.landmark_id, graph_note=graph_note,
            lang=intent.lang, profile=profile, **extra,
        ), False
    except Exception as exc:  # noqa: BLE001 - the cards are still useful without a route
        log.error("request %s: plan_route failed (%s)\n%s", request_id, type(exc).__name__,
                  _frames(exc))
        return None, True


def _event_stop(cards: list[Card], now: datetime) -> str | None:
    """The event card that may end the route: on now or starting within
    EVENT_STOP_MAX_WAIT_MIN; of several, the one that starts first."""
    soon = [c for c in cards if c.kind == "event" and c.starts_at is not None
            and c.starts_at - now <= timedelta(minutes=EVENT_STOP_MAX_WAIT_MIN)]
    return min(soon, key=lambda c: (c.starts_at, c.rank)).poi_id if soon else None


def _takes_last_stop() -> bool:
    try:
        return "last_stop_id" in inspect.signature(route.plan_route).parameters
    except (TypeError, ValueError):
        return False


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
    night_taxi: bool = False,
) -> list[str]:
    out: list[str] = []
    unknown = sum(1 for c in cards if c.open_status == "unknown" and c.kind == "place")
    if unknown == 1:
        out.append(t["hours_unknown_1"])
    elif unknown > 1:
        out.append(t["hours_unknown_n"].format(n=unknown))
    if intent.night:
        out.append(t["night_taxi"].format(v=intent.max_walk_min) if night_taxi else t["night"])
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
             night_by_clock: bool = False, semantic: bool = False,
             semantic_reason: str | None = None, claude_picked: bool = False,
             index_stale: bool = False, hours_dropped: dict[str, int] | None = None,
             profile: RouteProfile = "fastest", profile_auto: bool = False,
             profile_applied: bool = True,
             prefs: UserPrefs | None = None, warm: str | None = None,
             short_out: bool = False, events_open: bool = False,
             temp_c: float | None = None, excluded: int = 0) -> list[str]:
    """`semantic`: the query vector was computed. The Gemini quota can run out silently
    (embed_query then returns None), so the panel always says which search actually ran.
    `airport` = minutes to departure when the flight rule shaped the time budget.
    `profile` = the route profile asked (the default "fastest" is not listed); `profile_auto`:
    switched to "accessible" by the wheelchair. `prefs`: the person's likes / avoids, labels
    only (shown in the answer, never logged)."""
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
    if profile != "fastest":  # «запрошен …» when the route was straight / had to take stairs
        key = f"f_profile_{profile}" if profile_applied else f"f_profile_req_{profile}"
        out.append(t[key] + (t["f_profile_auto"] if profile_auto else ""))
    line = _prefs_line(t, prefs, lang)
    if line:
        out.append(line)
    # what the code did: indoor stops (warm), short walks outside (warm, avoids long_outdoor);
    # an explicit walk limit over WARM_MAX_LEG_MIN was kept: only «between places» holds
    capped = (intent.max_walk_min or 0) <= WARM_MAX_LEG_MIN
    if warm is not None:
        frost = warm == "frost" and temp_c is not None
        out.append(t["f_warm" if capped else "f_warm_between"].format(v=WARM_MAX_LEG_MIN)
                   + (t["f_warm_frost"].format(t=_temp(temp_c)) if frost else ""))
    elif short_out:
        out.append(t["f_short" if capped else "f_short_between"].format(v=WARM_MAX_LEG_MIN))
    if events_open:  # an event card outside the place categories: say why
        out.append(t["f_events_open"])
    if excluded:  # «Ещё»: a count only, the ids stay out of the answer text
        out.append(t["f_excluded"].format(n=excluded))
    if semantic:
        out.append(t["f_search_semantic"] + (f" ({t['index_stale']})" if index_stale else ""))
    else:
        line = t["f_search_lexical"]
        if semantic_reason:  # already worded: failures only (an empty key is not one)
            line += f" ({semantic_reason})"
        out.append(line + (t["f_search_claude"] if claude_picked else ""))
    names = HOURS_DROP_NAMES[lang]
    parts = [f"{names[k]} {hours_dropped[k]}" for k in names if (hours_dropped or {}).get(k)]
    if parts:
        total = sum(hours_dropped.get(k, 0) for k in names)  # type: ignore[union-attr]
        out.append(t["f_hours_dropped"].format(n=total, parts=", ".join(parts)))
    if not use_llm:
        out.append(t["f_no_ai"])
    return out


def _prefs_line(t: dict[str, str], prefs: UserPrefs | None, lang: Lang) -> str | None:
    """«предпочтения: музеи, без баров; избегать: кофе» in the answer language."""
    if prefs is None:
        return None
    names = PREF_NAMES[lang]
    parts = [t[key].format(v=", ".join(names[x] for x in tags))
             for key, tags in (("f_prefs_likes", prefs.likes), ("f_prefs_avoids", prefs.avoids))
             if tags]
    return "; ".join(parts) or None


def _sources(cards: list[Card], has_weather: bool) -> list[str]:
    sources = sorted({s for c in cards for s in c.facts_sources})
    if has_weather:
        sources.append("open_meteo")
    return sources
