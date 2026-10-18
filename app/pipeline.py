"""End-to-end recommendation flow. Owner: LLM lane (task T34).

recommend():
 1. resolve_origin(location)                       app.geo       (CORE)
 2. parse_query + apply_overrides                  app.query_parser
 3. context: local time, radius_for, weather       app.hours / app.geo / app.weather
 4. query_vec = llm.embed_query(...) or None
 5. retrieve(store, intent, ctx, query_vec, k=8)   app.retrieval (CORE)
    no candidates -> widen radius x1.5 once -> still none -> empty answer + helpful warning
 6. draft_cards (LLM) -> finalize                  app.generate + app.validate (CORE)
    LLMUnavailable / use_llm=False -> template_cards
 7. plan_route for >= 2 cards when want_route      app.route (CORE)
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
from datetime import datetime

import h3
import numpy as np

from app import geo, hours, retrieval, route, validate, weather
from app.config import Settings
from app.generate import draft_cards
from app.llm import LLMClient, LLMUnavailable
from app.query_parser import apply_overrides, parse_query
from app.schemas import (
    Answer,
    Candidate,
    Card,
    DataUsed,
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
WIDEN_FACTOR = 1.5
MAX_RADIUS_M = 5000  # SearchContext.radius_m upper bound

TEXTS: dict[str, dict[str, str]] = {
    "ru": {
        "llm_down": "LLM недоступна — показаны карточки по данным.",
        "hours_unknown_1": "Для 1 места нет данных о часах работы — проверьте перед визитом.",
        "hours_unknown_n": "Для {n} мест нет данных о часах работы — проверьте перед визитом.",
        "night": "Ночь: выбирайте освещённые людные места и заранее продумайте дорогу обратно.",
        "widened": "Рядом мало вариантов — радиус поиска расширен до {r} м.",
        "nothing": "Подходящих мест не нашлось. Попробуйте увеличить время, убрать часть "
                   "фильтров или выбрать другую точку.",
        "search_error": "Поиск временно дал сбой — попробуйте ещё раз.",
        "landmark_mismatch": "В запросе упомянут ориентир «{name}», но подборка построена "
                             "от выбранной точки.",
        "wheelchair": "Доступность для коляски подтверждена не для всех мест — уточните "
                      "перед визитом.",
        "tourist": "Заложите время на дорогу в аэропорт и регистрацию.",
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
        "night": "Түн: жарық әрі адамы көп жерлерді таңдаңыз, қайтар жолды алдын ала "
                 "ойластырыңыз.",
        "widened": "Жақын жерде нұсқа аз — іздеу радиусы {r} м-ге дейін ұлғайтылды.",
        "nothing": "Сәйкес орын табылмады. Уақытты көбейтіп, кейбір сүзгілерді алып тастап "
                   "немесе басқа нүкте таңдап көріңіз.",
        "search_error": "Іздеуде уақытша қате шықты — қайталап көріңіз.",
        "landmark_mismatch": "Сұрауда «{name}» аталған, бірақ іріктеме таңдалған нүктеден "
                             "жасалды.",
        "wheelchair": "Арбамен кіру мүмкіндігі барлық орын үшін расталмаған — барар алдында "
                      "нақтылаңыз.",
        "tourist": "Әуежайға жол мен тіркеуге уақыт қалдырыңыз.",
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
        "night": "Night: choose well-lit, busy places and plan your way back in advance.",
        "widened": "Few options nearby — search radius widened to {r} m.",
        "nothing": "No matching places found. Try more time, fewer filters or another "
                   "starting point.",
        "search_error": "Search failed temporarily — please try again.",
        "landmark_mismatch": "Your message mentions “{name}”, but results are built from the "
                             "selected starting point.",
        "wheelchair": "Wheelchair access is not confirmed for every place — check before you go.",
        "tourist": "Leave time to get to the airport and check in.",
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

    # 3. context
    local_time = _local_time(req.at)
    wx = weather.get_weather(origin, local_time)
    ctx = SearchContext(
        origin=origin,
        h3_cell=h3_cell,
        h3_res=h3.get_resolution(h3_cell),
        area_label=area_label,
        landmark_id=landmark_id,
        local_time=local_time,
        weather=wx,
        radius_m=_safe_radius(intent),
    )

    # 4. query vector (Gemini, optional; never in the no-AI mode)
    query_vec = None
    if req.use_llm and deps.llm is not None and store.embeddings is not None:
        query_vec = deps.llm.embed_query(intent.raw_query)

    # 5. retrieve, widen once
    candidates, failed = _retrieve(store, intent, ctx, query_vec)
    if not candidates and not failed:
        wider = min(round(ctx.radius_m * WIDEN_FACTOR), MAX_RADIUS_M)
        if wider > ctx.radius_m:
            ctx = ctx.model_copy(update={"radius_m": wider})
            candidates, failed = _retrieve(store, intent, ctx, query_vec)
            if candidates:
                warn.append(t["widened"].format(r=wider))

    # 6. cards
    cards: list[Card] = []
    generation = "template"
    summary: str | None = None
    gen_model: str | None = None
    if candidates and llm is not None:
        try:
            draft = draft_cards(intent, ctx, candidates, llm)
            cards, fin_warnings = validate.finalize(draft, candidates, intent, ctx)
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
            cards = validate.template_cards(candidates, intent, ctx)
        except Exception as exc:  # noqa: BLE001
            log.error("request %s: template_cards failed (%s)", request_id, type(exc).__name__)
            failed = True
    cards = cards[:3]

    # 7. route
    mini_route = _route(ctx, cards, candidates, intent, req.want_route, request_id)

    # 8. warnings, data used, answer
    if failed and not cards:
        warn.append(t["search_error"])
    elif not cards:
        warn.append(t["nothing"])
    warn.extend(_context_warnings(t, intent, cards, candidates, mini_route))
    if summary is None:
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
            weather=wx.summary if wx is not None else t["weather_unknown"],
            filters=_filters(t, intent, ctx, req.use_llm),
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
) -> MiniRoute | None:
    if not want_route or len(cards) < 2:
        return None
    by_id = {c.poi.id: c for c in candidates}
    stops = [by_id[c.poi_id] for c in cards if c.poi_id in by_id]
    try:
        return route.plan_route(
            ctx.origin, stops, intent.time_budget_min, {c.poi_id: c.visit_min_low for c in cards}
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
) -> list[str]:
    out: list[str] = []
    unknown = sum(1 for c in cards if c.open_status == "unknown")
    if unknown == 1:
        out.append(t["hours_unknown_1"])
    elif unknown > 1:
        out.append(t["hours_unknown_n"].format(n=unknown))
    if intent.night:
        out.append(t["night"])
    if intent.wheelchair and cards:
        wheel = {c.poi.id: c.poi.wheelchair for c in candidates}
        if any(wheel.get(c.poi_id, "unknown") != "yes" for c in cards):
            out.append(t["wheelchair"])
    if intent.tourist and cards:
        out.append(t["tourist"])
    if mini_route is not None and not mini_route.fits_budget:
        out.append(t["route_over"])
    return out


def _template_summary(t: dict[str, str], n: int) -> str:
    if n == 0:
        return t["summary_empty"]
    return t["summary_1"] if n == 1 else t["summary_n"].format(n=n)


def _time_label(t: dict[str, str], local_time: datetime, *, manual: bool) -> str:
    label = f"{local_time:%d.%m.%Y %H:%M} (UTC+5)"
    return f"{label} · {t['time_manual']}" if manual else label


def _filters(t: dict[str, str], intent: QueryIntent, ctx: SearchContext, use_llm: bool
             ) -> list[str]:
    out = [t["f_radius"].format(v=ctx.radius_m)]
    if intent.time_budget_min:
        out.append(t["f_budget"].format(v=intent.time_budget_min))
    if intent.max_walk_min:
        out.append(t["f_walk"].format(v=intent.max_walk_min))
    flags = (("f_kids", intent.with_kids), ("f_wheelchair", intent.wheelchair),
             ("f_indoor", intent.indoor == "indoor"), ("f_outdoor", intent.indoor == "outdoor"),
             ("f_night", intent.night), ("f_tourist", intent.tourist))
    out.extend(t[key] for key, on in flags if on)
    if intent.group_size:
        out.append(t["f_group"].format(v=intent.group_size))
    if intent.categories:
        out.append(t["f_categories"].format(v=", ".join(intent.categories)))
    if intent.must_tags:
        out.append(t["f_tags"].format(v=", ".join(intent.must_tags)))
    if not use_llm:
        out.append(t["f_no_ai"])
    return out


def _sources(cards: list[Card], has_weather: bool) -> list[str]:
    sources = sorted({s for c in cards for s in c.facts_sources})
    if has_weather:
        sources.append("open_meteo")
    return sources
