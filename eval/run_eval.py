"""Scenario eval for Qadam (EVAL lane, T51).

Usage:
  python -m eval.run_eval --mock              # every scenario gets web/mock/answer_sample.json
  python -m eval.run_eval                     # in-process app.pipeline.recommend + real Store
  python -m eval.run_eval --no-llm            # same, but use_llm=false in every request
  python -m eval.run_eval --url http://localhost:8000
  python -m eval.run_eval --smoke [--no-llm | --url URL | --mock]   # team queries, no grading
Options: --only S1,E3   --out DIR (default eval/)   --queries CSV (default eval/queries_team.csv)

Checks use only fields of app.schemas.Answer. Writes report.md + report.json.
--smoke runs every query from the CSV without strict checks and writes smoke_report.md
for reading by eye (common invariants are listed as remarks, not failures).
Exit code is always 0: the summary line "EVAL <mode>: N/16" is the result.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import math
import re
import statistics
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from app.schemas import Answer, RecommendRequest
from app.validate import has_open_claim

ROOT = Path(__file__).resolve().parent.parent
SCENARIOS_PATH = Path(__file__).resolve().parent / "scenarios.yaml"
MOCK_PATH = ROOT / "web" / "mock" / "answer_sample.json"

NIGHT_TAGS = {"late_open", "24_7"}
ACCESS_MARKERS = ("коляск", "доступн", "не подтвержд", "wheelchair", "accessib", "кедергісіз")
HOURS_MARKERS = ("нет данных", "не подтвержд", "проверьте", "no data", "not confirmed", "check",
                 "дерек жоқ", "тексеріңіз")
KK_LETTERS = set("әғқңөұүһі")
SENTENCE_END = re.compile(r"[.!?…]+(?:\s|$)")
# 1.3.0: lit=yes is OSM data, not a safety promise; an accessible route with steps says why
SAFETY_WORDS = ("безопасн", "safe", "қауіпсіз")
STEPS_MARKERS = ("ступен", "лестниц", "step", "stair", "баспалдақ")
LIVE_MARKERS = ("идёт сейчас", "идет сейчас", "уже идёт", "уже идет", "happening now",
                "on now", "қазір өтіп")
# prefs reach a log line only through a field name: a dump of the request or of UserPrefs, or
# an f-string with the field (repr(RecommendRequest) leaves prefs out, repr=False). Bare tag
# words are not searched: night, coffee, kids are also the parser's own tags (must_tags).
PREFS_LOG_MARKERS = ("prefs", "likes", "avoids", "userprefs")


# --------------------------------------------------------------------- outcomes


@dataclass
class Outcome:
    """What a runner got for one request."""

    answer: Answer | None = None
    rejected: bool = False  # 422 / KeyError / ValueError: invalid input handled correctly
    error: str | None = None  # crash, 5xx, invalid JSON — always a failure
    latency_ms: int = 0
    search: str | None = None  # "vector" | "words"; None = unknown (see search_mode)
    baseline: Answer | None = None  # the same request without prefs (expect.prefs_shift)
    # in-process only: (logger name, message) of every log record written during the run
    log_lines: list[tuple[str, str]] | None = None
    reject_body: str | None = None  # --url only: the 422 body (must not echo the input)


@dataclass
class Result:
    id: str
    title: str
    expected_type: str
    passed: bool
    failures: list[str] = field(default_factory=list)
    latency_ms: int = 0
    cards: int = 0
    generation: str | None = None
    search: str | None = None  # "vector" (embed_query returned a vector) | "words" | None
    search_line: str | None = None  # the «поиск: …» line from data_used.filters, verbatim
    route_source: str | None = None  # MiniRoute.path_source: "graph" | "straight"; None = no route
    wave: int | None = None  # 8 = wave 8 scenario (E13–E18); None = the 16 of the submission


Runner = Callable[[RecommendRequest], Outcome]


def load_scenarios(path: Path = SCENARIOS_PATH) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)


# ----------------------------------------------------------------------- checks


def _card_text(card: Any) -> str:
    return f"{card.why} {card.what_to_do}"


def _all_text(ans: Answer) -> str:
    parts = [ans.summary, *ans.warnings]
    for c in ans.cards:
        parts += [c.title, c.why, c.what_to_do, c.open_note]
    return " ".join(parts)


def _sentences(text: str) -> int:
    return len([s for s in SENTENCE_END.split(text.strip()) if s.strip()])


def detect_script(text: str) -> str:
    """Rough language guess by alphabet: kk (Cyrillic + kk letters) / ru / en."""
    letters = [ch for ch in text.lower() if ch.isalpha()]
    if not letters:
        return "unknown"
    cyr = sum("а" <= ch <= "я" or ch == "ё" or ch in KK_LETTERS for ch in letters)
    if cyr / len(letters) < 0.5:
        return "en"
    return "kk" if any(ch in KK_LETTERS for ch in letters) else "ru"


def check_invariants(ans: Answer) -> list[str]:
    """Rules that hold for every answer, whatever the scenario."""
    fails: list[str] = []
    ranks = [c.rank for c in ans.cards]
    if len(set(ranks)) != len(ranks):
        fails.append(f"повтор rank: {ranks}")
    ids = [c.poi_id for c in ans.cards]
    if len(set(ids)) != len(ids):
        fails.append("одно место в двух карточках")
    titles = [c.title.strip().lower() for c in ans.cards]
    if len(set(titles)) != len(titles):  # 110 of 111 playgrounds are «Детская площадка»
        fails.append(f"одинаковые названия карточек: {[c.title for c in ans.cards]}")
    warnings = " ".join(ans.warnings).lower()
    for c in ans.cards:
        if c.open_status == "unknown" and c.open_confidence == "high":
            fails.append(f"{c.poi_id}: unknown с уверенностью high")
        if c.open_status == "unknown" and not any(
            m in f"{c.open_note} {warnings}".lower() for m in HOURS_MARKERS
        ):  # hours exist for only 28% of POI: the card must say so
            fails.append(f"{c.poi_id}: часы неизвестны, но нет пометки")
        if _sentences(c.why) > 2:
            fails.append(f"{c.poi_id}: why длиннее двух предложений")
        if c.visit_min_low > c.visit_min_high:
            fails.append(f"{c.poi_id}: visit_min_low > visit_min_high")
    if ans.route is not None and not set(ans.route.stop_ids) <= set(ids):
        fails.append("маршрут ведёт в места не из карточек")
    fails += check_graph_route(ans)
    fails += check_route_facts(ans)
    fails += check_crossing_kinds(ans, crossing_node_flags())
    fails += check_event_cards(ans)
    fails += check_straight_share(ans)
    fails += check_event_stops(ans)
    fails += check_event_venue(ans, event_venues())
    return fails


# ------------------------------------------------------ wave 8 (contract 1.3.0)

GRAPH_PATH = ROOT / "data" / "processed" / "walk_graph.npz"
NODE_CROSSING, NODE_SIGNALS, NODE_UNMARKED, NODE_MARKINGS_KNOWN = 1, 2, 8, 32
# The crossing check compares with the LOCAL graph file. main() turns it off for --url
# (the server may run another graph) unless --local-data-check says the data are the same.
CROSSING_GRAPH: Path | None = GRAPH_PATH
_node_flags_cache: dict[Path, dict[tuple[float, float], int] | None] = {}
_graph_load_errors: dict[Path, str] = {}


def crossing_node_flags(path: Path | None = None) -> dict[tuple[float, float], int] | None:
    """Flags of the crossing nodes of the walk graph by their point. None: the check is off,
    or no file / unreadable file (see crossing_check_note). Default path: CROSSING_GRAPH.
    The route puts a crossing node at the node's own float32 coordinates."""
    path = path if path is not None else CROSSING_GRAPH
    if path is None:
        return None
    if path not in _node_flags_cache:
        try:
            import numpy as np

            with np.load(path) as g:
                lat, lon = g["node_lat"].astype(float), g["node_lon"].astype(float)
                flags = g["node_flags"].astype(int)
            idx = np.nonzero(flags & NODE_CROSSING)[0]
            _node_flags_cache[path] = {
                (round(lat[i], 6), round(lon[i], 6)): int(flags[i]) for i in idx}
        except FileNotFoundError:
            _node_flags_cache[path] = None
            _graph_load_errors[path] = "нет файла"
        except Exception as exc:  # noqa: BLE001 — a broken file is reported, not raised
            _node_flags_cache[path] = None
            _graph_load_errors[path] = f"файл не читается ({type(exc).__name__})"
    return _node_flags_cache[path]


def crossing_check_note(url_mode: bool) -> str | None:
    """One report line when the crossing check did not run, or ran against a local file."""
    if CROSSING_GRAPH is None:
        return ("переходы «зебра» не сверены с графом: --url, у сервера может быть другой "
                "граф (включить: --local-data-check, если граф тот же)")
    crossing_node_flags()
    err = _graph_load_errors.get(CROSSING_GRAPH)
    if err:
        return f"переходы «зебра» не сверены с графом: {CROSSING_GRAPH.name} — {err}"
    if url_mode:
        return (f"переходы «зебра» сверены с локальным {CROSSING_GRAPH.name} "
                "(--local-data-check)")
    return None


def check_crossing_kinds(ans: Answer, nodes: dict[tuple[float, float], int] | None) -> list[str]:
    """1.3.0: «marked» (zebra) only for a crossing node whose markings are known (bit 32,
    no signals / unmarked bits). A crossing without markings data must be «unknown»."""
    route = ans.route
    if route is None or nodes is None:
        return []
    bad = 0
    for x in route.crossings:
        if x.kind != "marked":
            continue
        flags = nodes.get((round(x.lat, 6), round(x.lon, 6)))
        if flags is None or not flags & NODE_MARKINGS_KNOWN \
                or flags & (NODE_SIGNALS | NODE_UNMARKED):
            bad += 1
    if bad:
        return [f"переходов без данных о разметке показано как «зебра»: {bad} из "
                f"{len(route.crossings)}"]
    return []


def check_route_facts(ans: Answer) -> list[str]:
    """1.3.0 route facts, when present, must agree with the route itself."""
    route = ans.route
    if route is None:
        return []
    fails: list[str] = []
    shares = (route.lit_share, route.unlit_share, route.unknown_lit_share)
    if route.path_source == "straight" and route.unknown_lit_share is not None \
            and route.unknown_lit_share < 0.98:
        fails.append(f"прямые отрезки, а доли освещённости {shares}, ожидали 0/0/1")
    if route.max_outdoor_leg_min is not None and route.legs:
        longest = max(leg.walk_min for leg in route.legs)
        if route.max_outdoor_leg_min > longest:
            fails.append(f"max_outdoor_leg_min {route.max_outdoor_leg_min} > самого длинного "
                         f"отрезка {longest} мин")
    if route.kerb_lowered is not None and route.kerb_unknown is not None \
            and len(route.crossings) < 50 \
            and route.kerb_lowered + route.kerb_unknown > len(route.crossings):
        fails.append(f"бордюров {route.kerb_lowered}+{route.kerb_unknown} больше переходов "
                     f"{len(route.crossings)}")
    note = route.note.lower()
    if route.profile == "accessible" and route.steps_count > 0 \
            and not any(m in note for m in STEPS_MARKERS):
        fails.append(f"без ступенек, но ступеней {route.steps_count}, а в note нет причины")
    if any(w in note for w in SAFETY_WORDS):
        fails.append(f"в note маршрута освещённость подана как безопасность: «{route.note[:80]}»")
    return fails


def check_event_cards(ans: Answer) -> list[str]:
    """Event cards: source «manual», not past, «идёт сейчас» only while it is on."""
    now = ans.context.local_time
    fails: list[str] = []
    for c in ans.cards:
        if c.kind != "event" or c.starts_at is None:
            continue
        if "manual" not in c.facts_sources:
            fails.append(f"{c.poi_id}: у события facts_sources {c.facts_sources}, ожидали manual")
        if c.ends_at is not None and c.ends_at <= now:
            fails.append(f"{c.poi_id}: событие уже закончилось ({c.ends_at:%d.%m %H:%M})")
        if c.ends_at is None and c.starts_at.astimezone(now.tzinfo).date() < now.date():
            fails.append(f"{c.poi_id}: событие было {c.starts_at:%d.%m}, раньше запроса")
        on_now = c.ends_at is not None and c.starts_at <= now < c.ends_at
        text = f"{_card_text(c)} {c.open_note}".lower()
        if not on_now and any(m in text for m in LIVE_MARKERS):
            fails.append(f"{c.poi_id}: «идёт сейчас», хотя starts_at–ends_at этого не дают")
    return fails


# 1.3.1: «без ступенек» in data_used.filters (ru / kk / en, app/pipeline.py f_profile_accessible);
# «запрошен маршрут без ступенек» (asked, not achieved) is not a claim
STEP_FREE_CLAIMS = ("маршрут: без ступенек", "маршрут: баспалдақсыз", "route: step-free")
EVENT_STOP_MAX_WAIT_MIN = 30  # an event starting later than this is not a route stop (1.3.1)
EVENTS_PATH = ROOT / "data" / "events.json"
# like CROSSING_GRAPH: off for --url unless --local-data-check (the server may have other events)
EVENTS_FILE: Path | None = EVENTS_PATH
_event_venues_cache: dict[Path, dict[str, str] | None] = {}
_events_load_errors: dict[Path, str] = {}


def check_straight_share(ans: Answer) -> list[str]:
    """1.3.1: straight_share is 1 on a straight route; «без ступенек» only on a graph route with
    no steps and no straight segments (the text of note is not used for this)."""
    route = ans.route
    fails: list[str] = []
    if route is not None and route.straight_share is not None \
            and route.path_source == "straight" and route.straight_share < 0.999:
        fails.append(f"прямой маршрут, а straight_share {route.straight_share}, ожидали 1")
    claims = [f for f in ans.data_used.filters if any(m in f.lower() for m in STEP_FREE_CLAIMS)]
    if claims:
        ok = (route is not None and route.path_source == "graph" and route.steps_count == 0
              and route.straight_share == 0)
        if not ok:
            got = "нет маршрута" if route is None else (
                f"{route.path_source}, ступеней {route.steps_count}, "
                f"straight_share {route.straight_share}")
            fails.append(f"«без ступенек» в filters, а маршрут: {got}")
    return fails


def check_event_stops(ans: Answer) -> list[str]:
    """1.3.1: an event in the route is the last stop, and only if it starts within 30 min."""
    route = ans.route
    if route is None:
        return []
    now = ans.context.local_time
    fails: list[str] = []
    for c in ans.cards:
        if c.kind != "event" or c.starts_at is None or c.poi_id not in route.stop_ids:
            continue
        if route.stop_ids[-1] != c.poi_id:
            fails.append(f"{c.poi_id}: событие — не последняя остановка ({route.stop_ids})")
        wait_min = (c.starts_at - now).total_seconds() / 60
        if wait_min > EVENT_STOP_MAX_WAIT_MIN:
            fails.append(f"{c.poi_id}: до начала {wait_min:.0f} мин > "
                         f"{EVENT_STOP_MAX_WAIT_MIN}, а событие — остановка маршрута")
    return fails


def event_venues(path: Path | None = None) -> dict[str, str] | None:
    """{event id: venue} from the local data/events.json. None: the check is off (--url),
    no file or unreadable file (see events_check_note). Default path: EVENTS_FILE."""
    path = path if path is not None else EVENTS_FILE
    if path is None:
        return None
    if path not in _event_venues_cache:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            _event_venues_cache[path] = {
                str(e["id"]): str(e["venue"]) for e in raw
                if isinstance(e, dict) and e.get("id") and e.get("venue")}
        except FileNotFoundError:
            _event_venues_cache[path] = None
            _events_load_errors[path] = "нет файла"
        except (OSError, ValueError, TypeError) as exc:
            _event_venues_cache[path] = None
            _events_load_errors[path] = f"файл не читается ({type(exc).__name__})"
    return _event_venues_cache[path]


def events_check_note(url_mode: bool) -> str | None:
    """One report line when event venues were not checked, or checked against a local file."""
    if EVENTS_FILE is None:
        return ("площадки событий не сверены с data/events.json: --url, у сервера могут быть "
                "другие события (включить: --local-data-check, если данные те же)")
    event_venues()
    err = _events_load_errors.get(EVENTS_FILE)
    if err:
        return f"площадки событий не сверены: {EVENTS_FILE.name} — {err}"
    if url_mode:
        return f"площадки событий сверены с локальным {EVENTS_FILE.name} (--local-data-check)"
    return None


def check_event_venue(ans: Answer, venues: dict[str, str] | None) -> list[str]:
    """1.3.1: an event card carries Card.venue when the venue is known in data/events.json."""
    if venues is None:
        return []
    return [f"{c.poi_id}: у события нет venue, а площадка известна («{venues[c.poi_id]}»)"
            for c in ans.cards
            if c.kind == "event" and not c.venue and c.poi_id in venues]


def _prefs_score(ans: Answer, spec: dict[str, Any]) -> int:
    """Cards in liked categories or with a liked tag, minus cards in avoided categories."""
    likes, avoids = spec.get("likes_categories", []), spec.get("avoid_categories", [])
    tags = set(spec.get("likes_tags", []))
    return sum(
        (c.category in likes or bool(tags & set(c.tags))) - (c.category in avoids)
        for c in ans.cards
    )


def check_prefs_shift(ans: Answer, base: Answer | None, spec: dict[str, Any]) -> list[str]:
    """Prefs must change the pick: more cards in liked categories, fewer in avoided ones,
    than the same request without prefs."""
    if base is None:
        return ["нет ответа без prefs для сравнения"]
    got, was = _prefs_score(ans, spec), _prefs_score(base, spec)
    if got > was:
        return []
    cats = [c.category for c in ans.cards]
    base_cats = [c.category for c in base.cards]
    if was == len(base.cards) and base.cards:
        return [f"сценарий не различает: и без prefs все карточки по вкусу ({base_cats})"]
    return [f"prefs не сдвинули подбор: с prefs {cats}, без {base_cats}"]


def check_prefs_not_logged(
    records: list[tuple[str, str]] | None, req: dict[str, Any]
) -> list[str]:
    """No log record may carry prefs together with the cell, the landmark or the query.
    The report names the record number and the logger only: the line itself may hold the
    query and the cell, so it is never copied."""
    if records is None:
        return []  # --url / mock: server logs are not visible to eval
    loc = req.get("location") or {}
    where = [v for v in (loc.get("h3_cell"), loc.get("landmark_id"), req.get("query")) if v]
    fails = []
    for n, (logger, line) in enumerate(records, 1):
        low = line.lower()
        if any(m in low for m in PREFS_LOG_MARKERS) and any(w.lower() in low for w in where):
            fails.append(f"prefs в логе вместе с местом или запросом: запись №{n}, "
                         f"логгер {logger}")
    return fails


# ------------------------------------------------------------- walking route (1.1.0)

ROUTE_END_TOLERANCE_M = 50  # path ends vs start / last place; graph nodes are not the POI point
WALK_M_PER_MIN = 75.0  # 4.5 km/h, the same speed as app.geo
STRAIGHT_TOLERANCE_M = 3.0  # the route may measure length with a flat approximation, eval with
# haversine: near a multiple of 75 m a nearly straight leg would fail by a rounding hair


def min_walk_minutes(straight_m: float) -> int:
    """Lower bound for a graph leg: walking the straight line, minus the tolerance."""
    return math.ceil(max(0.0, straight_m - STRAIGHT_TOLERANCE_M) / WALK_M_PER_MIN - 1e-9)
CROSSING_KINDS = {"signals", "marked", "unmarked", "unknown"}


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6_371_000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(h)))


def check_graph_route(ans: Answer) -> list[str]:
    """Checks for a route along the OSM walk graph (path_source="graph"); straight routes and
    answers without a route are not checked here (WALK_GRAPH=false keeps the old behaviour)."""
    route = ans.route
    if route is None or route.path_source != "graph":
        return []
    fails: list[str] = []
    origin = ans.context.origin
    points = {"origin": (origin.lat, origin.lon)}
    points.update({c.poi_id: (c.geo.lat, c.geo.lon) for c in ans.cards})
    if len(route.path) < 2:
        return ["граф: в path меньше двух точек"]
    first, last = route.path[0], route.path[-1]
    d_start = haversine_m(first.lat, first.lon, *points["origin"])
    if d_start > ROUTE_END_TOLERANCE_M:
        fails.append(f"граф: path начинается в {d_start:.0f} м от старта")
    end_id = route.legs[-1].to_id if route.legs else route.stop_ids[-1]
    if end_id in points:  # the last leg may lead back to the start (loop walk)
        d_end = haversine_m(last.lat, last.lon, *points[end_id])
        if d_end > ROUTE_END_TOLERANCE_M:
            fails.append(f"граф: path заканчивается в {d_end:.0f} м от {end_id}")
    legs_m = sum(leg.distance_m for leg in route.legs)
    if abs(legs_m - route.total_walk_m) > max(1, len(route.legs)):  # rounding per leg
        fails.append(f"граф: сумма legs {legs_m} м ≠ total_walk_m {route.total_walk_m} м")
    for leg in route.legs:
        if leg.from_id not in points or leg.to_id not in points:
            fails.append(f"граф: leg {leg.from_id}→{leg.to_id} ведёт не в карточку и не к старту")
            continue
        straight = haversine_m(*points[leg.from_id], *points[leg.to_id])
        floor_min = min_walk_minutes(straight)
        if leg.walk_min < floor_min:
            fails.append(f"граф: leg {leg.from_id}→{leg.to_id} {leg.walk_min} мин "
                         f"< по прямой {floor_min} мин")
    if len(route.crossings) > 50:
        fails.append(f"граф: переходов {len(route.crossings)} > 50")
    bad_kinds = {x.kind for x in route.crossings} - CROSSING_KINDS
    if bad_kinds:
        fails.append(f"граф: неизвестный kind перехода {sorted(bad_kinds)}")
    return fails


def _check_lang(ans: Answer, want: str) -> list[str]:
    fails = []
    if ans.lang != want:
        fails.append(f"lang={ans.lang}, ожидали {want}")
    if ans.cards:
        got = detect_script(" ".join(_card_text(c) for c in ans.cards))
        if got != want:
            fails.append(f"тексты карточек похожи на {got}, ожидали {want}")
    return fails


def check_expect(ans: Answer, exp: dict[str, Any], graph_on: bool = True) -> list[str]:
    """Scenario-specific checks; keys are documented in eval/scenarios.yaml."""
    fails: list[str] = []
    n = len(ans.cards)
    if "min_cards" in exp and n < exp["min_cards"]:
        fails.append(f"карточек {n} < {exp['min_cards']}")
    if "max_cards" in exp and n > exp["max_cards"]:
        fails.append(f"карточек {n} > {exp['max_cards']}")
    if "max_walk_min" in exp:
        far = [f"{c.poi_id}={c.walk_min}" for c in ans.cards if c.walk_min > exp["max_walk_min"]]
        if far:
            fails.append(f"пешком > {exp['max_walk_min']} мин: {', '.join(far)}")
    if "categories_any" in exp:
        bad = [c.category for c in ans.cards if c.category not in exp["categories_any"]]
        if bad:
            fails.append(f"категории не по запросу: {bad}")
    if "categories_forbid" in exp:
        bad = [c.category for c in ans.cards if c.category in exp["categories_forbid"]]
        if bad:
            fails.append(f"запрещённые категории: {bad}")
    if "lang" in exp:
        fails += _check_lang(ans, exp["lang"])

    route = exp.get("route")
    if route == "required" and ans.route is None:
        fails.append("нет маршрута")
    if route == "none" and ans.route is not None:
        fails.append("маршрут не ожидался")
    if "route_source" in exp and (graph_on or exp["route_source"] != "graph"):
        # graph: the route must follow the walk graph (1.1.0); skipped when the run has no graph
        got = ans.route.path_source if ans.route is not None else None
        if got != exp["route_source"]:
            fails.append(f"маршрут {got or 'нет'}, ожидали {exp['route_source']}")
    if exp.get("route_fits_budget") and ans.route is not None and not ans.route.fits_budget:
        fails.append(f"маршрут не укладывается в бюджет ({ans.route.total_min} мин)")

    if exp.get("labels_required"):
        for c in ans.cards:
            if not c.facts_sources or not c.open_note.strip():
                fails.append(f"{c.poi_id}: нет меток источника или пометки о часах")
    if exp.get("forbid_open_claim_without_high"):
        # the same guard validate uses (ru/kk/en roots + markers, NFKC, homoglyphs)
        for c in ans.cards:
            if c.open_status == "open" and c.open_confidence == "high":
                continue
            all_day = "24_7" in c.tags  # «круглосуточно» from the data is not a claim
            texts = (c.why, c.what_to_do, c.open_note)
            if any(has_open_claim(t, all_day_ok=all_day) for t in texts):
                fails.append(f"{c.poi_id}: «открыто сейчас» без open/high")
        all_open = bool(ans.cards) and all(
            c.open_status == "open" and c.open_confidence == "high" for c in ans.cards
        )
        if not all_open and has_open_claim(ans.summary):
            fails.append("summary: «открыто сейчас», хотя не все места open/high")
    if exp.get("warnings_required") and not ans.warnings:
        fails.append("нет предупреждений")
    if "warnings_any" in exp:
        joined = " ".join(ans.warnings).lower()
        if not any(s.lower() in joined for s in exp["warnings_any"]):
            fails.append(f"нет предупреждения с {exp['warnings_any']}")
    if exp.get("night_safe"):
        for c in ans.cards:
            if c.open_status != "open" and not NIGHT_TAGS & set(c.tags):
                fails.append(f"{c.poi_id}: ночью без подтверждения, что открыто")
    if exp.get("wheelchair_honest"):
        warned = any(m in " ".join(ans.warnings).lower() for m in ACCESS_MARKERS)
        for c in ans.cards:
            noted = any(m in f"{c.open_note} {_card_text(c)}".lower() for m in ACCESS_MARKERS)
            if "wheelchair" not in c.tags and not (warned or noted):
                fails.append(f"{c.poi_id}: доступность не подтверждена и не помечена")
    if "text_forbid" in exp:
        text = _all_text(ans).lower()
        hits = [s for s in exp["text_forbid"] if s.lower() in text]
        if hits:
            fails.append(f"в тексте запрещённое: {hits}")
    if "text_any" in exp:
        text = " ".join([ans.summary, *ans.warnings]).lower()
        if not any(s.lower() in text for s in exp["text_any"]):
            fails.append(f"в summary/warnings нет ни одного из {exp['text_any']}")
    # 1.3.0: route profile, route facts, «в тепле», events
    if "route_profile" in exp:
        if ans.route is None:
            fails.append(f"нет маршрута: профиль {exp['route_profile']} не проверить")
        elif ans.route.profile != exp["route_profile"]:
            fails.append(f"маршрут {ans.route.profile}, ожидали {exp['route_profile']}")
    if exp.get("route_facts") and ans.route is not None:
        r = ans.route
        facts = [("доли освещённости", r.lit_share), ("max_outdoor_leg_min", r.max_outdoor_leg_min)]
        if r.path_source == "graph":  # kerbs are None on straight segments (contract 1.3.0)
            facts.append(("бордюры", r.kerb_lowered))
        missing = [name for name, v in facts if v is None]
        if missing:
            fails.append(f"в маршруте нет фактов 1.3.0: {', '.join(missing)}")
    if "max_outdoor_leg_min" in exp and ans.route is not None:
        got = ans.route.max_outdoor_leg_min
        if got is None or got > exp["max_outdoor_leg_min"]:
            fails.append(f"на улице подряд {got} мин, ожидали ≤ {exp['max_outdoor_leg_min']}")
    if "indoor_cards_min" in exp:
        indoor = sum("indoor" in c.tags for c in ans.cards)
        if indoor < exp["indoor_cards_min"]:
            fails.append(f"мест в помещении {indoor} < {exp['indoor_cards_min']}")
    if exp.get("event_required") and not any(c.kind == "event" for c in ans.cards):
        fails.append("нет карточки события")
    # ranking probes (eval/probes.yaml, T70): what comes first matters, not just what is there
    top = ans.cards[0] if ans.cards else None
    if "top1_categories_any" in exp and top is not None:
        if top.category not in exp["top1_categories_any"]:
            fails.append(f"первая карточка {top.category} ({top.title}), ожидали "
                         f"{exp['top1_categories_any']}")
    if "top1_why_any" in exp and top is not None:
        if not any(w.lower() in top.why.lower() for w in exp["top1_why_any"]):
            fails.append(f"в why первой карточки нет ни одного из {exp['top1_why_any']}")
    if "top1_note_forbid" in exp and top is not None:
        hits = [s for s in exp["top1_note_forbid"] if s.lower() in top.open_note.lower()]
        if hits:
            fails.append(f"первая карточка: {hits} в «{top.open_note[:60]}»")
    if "notes_lang" in exp:
        for c in ans.cards:
            got = detect_script(c.open_note)
            if got != exp["notes_lang"]:
                fails.append(f"{c.poi_id}: пометка о часах на {got}: «{c.open_note[:40]}»")
    if "unknown_note_lang" in exp:
        for c in ans.cards:
            got = detect_script(c.open_note)
            if c.open_status == "unknown" and got != exp["unknown_note_lang"]:
                fails.append(f"{c.poi_id}: пометка о часах на {got}: «{c.open_note[:40]}»")
    if "intent_categories_include" in exp:
        lacking = [c for c in exp["intent_categories_include"] if c not in ans.intent.categories]
        if lacking:
            fails.append(f"intent.categories без {lacking}: {ans.intent.categories}")
    if "intent_categories_exclude" in exp:
        extra = [c for c in exp["intent_categories_exclude"] if c in ans.intent.categories]
        if extra:
            fails.append(f"intent.categories содержит {extra}: {ans.intent.categories}")
    for key, want in (exp.get("intent") or {}).items():
        got = getattr(ans.intent, key, None)
        if got != want:
            fails.append(f"intent.{key}={got!r}, ожидали {want!r}")
    for key, want in (exp.get("data_used") or {}).items():
        got = getattr(ans.data_used, key, None)
        if got != want:
            fails.append(f"data_used.{key}={got!r}, ожидали {want!r}")
    return fails


def grade(scn: dict[str, Any], out: Outcome, graph_on: bool = True) -> Result:
    exp = scn.get("expect") or {}
    res = Result(
        id=scn["id"],
        title=scn["title"],
        expected_type=scn.get("expected_type", ""),
        passed=False,
        latency_ms=out.latency_ms,
        search=search_mode(out),
        search_line=search_line(out),
        wave=scn.get("wave"),
    )
    if out.error:
        res.failures.append(f"ошибка: {out.error}")
        return res
    if out.rejected:
        if not (exp.get("reject_ok") or exp.get("reject_required")):
            res.failures.append("запрос отклонён (422), а ожидали ответ")
        echoed = [s for s in exp.get("reject_body_forbid", [])
                  if out.reject_body is not None and s.lower() in out.reject_body.lower()]
        if echoed:
            res.failures.append(f"ответ 422 повторяет ввод: {echoed}")
        res.passed = not res.failures
        return res
    ans = out.answer
    assert ans is not None
    res.cards = len(ans.cards)
    res.generation = ans.data_used.generation
    res.route_source = ans.route.path_source if ans.route is not None else None
    if exp.get("reject_required"):
        res.failures.append("ожидали 422, а пришёл ответ")
    elif exp.get("reject_ok"):
        if ans.cards or not ans.warnings:
            res.failures.append("ожидали 422 или 0 карточек с понятным предупреждением")
    else:
        res.failures += check_invariants(ans)
        res.failures += check_expect(ans, exp, graph_on)
        if "prefs_shift" in exp:
            res.failures += check_prefs_shift(ans, out.baseline, exp["prefs_shift"])
        if scn["request"].get("prefs"):
            res.failures += check_prefs_not_logged(out.log_lines, scn["request"])
    res.passed = not res.failures
    return res


# ---------------------------------------------------------------------- runners


def mock_runner() -> Runner:
    """Same canned Answer for every request: checks the eval itself, not the system."""
    raw = json.loads(MOCK_PATH.read_text(encoding="utf-8"))
    answer = Answer.model_validate(raw)

    def run(req: RecommendRequest) -> Outcome:
        return Outcome(answer=answer.model_copy(deep=True), latency_ms=answer.latency_ms)

    return run


def inprocess_runner(no_llm: bool) -> tuple[Runner | None, list[str]]:
    """app.pipeline.recommend with the real Store. Returns (runner | None, notes)."""
    notes: list[str] = []
    try:
        from app.config import get_settings
        from app.llm import LLMClient
        from app.pipeline import Deps, recommend
        from app.store import load_store

        settings = get_settings()
        store = load_store(settings)
        llm = None if no_llm or not settings.has_llm else LLMClient(settings)
    except NotImplementedError as exc:
        return None, [f"pipeline ещё не готов: {exc}"]
    except Exception as exc:  # noqa: BLE001 — setup failure is reported, not raised
        return None, [f"не удалось поднять pipeline: {type(exc).__name__}: {exc}"]
    if llm is None and not no_llm:
        notes.append("LLM недоступна (нет ключа или LLM_ENABLED=0): ответы шаблонные")
    if store.embeddings is None:
        emb = "нет, лексика"
    elif no_llm:
        emb = "есть, но в режиме без ИИ не используются (лексика)"
    else:
        emb = "есть"
    notes.append(f"POI в Store: {len(store.pois)}; эмбеддинги: {emb}")
    notes += [f"Store: {w}" for w in store.warnings]
    deps = Deps(store=store, llm=llm, settings=settings)
    got_vector: list[bool] = []  # set by the embed_query wrapper during one request
    if llm is not None:
        real_embed = llm.embed_query

        def counting_embed(text: str) -> Any:
            vec = real_embed(text)
            got_vector.append(vec is not None)
            return vec

        llm.embed_query = counting_embed  # type: ignore[method-assign]

    def run(req: RecommendRequest) -> Outcome:
        got_vector.clear()
        t0 = time.perf_counter()
        with capture_logs() as lines:
            try:
                ans = recommend(req, deps)
                out = Outcome(answer=ans, latency_ms=_ms(t0),
                              search="vector" if any(got_vector) else "words")
            except (KeyError, ValueError, ValidationError):
                out = Outcome(rejected=True, latency_ms=_ms(t0))
            except Exception as exc:  # noqa: BLE001 — a crash is a failed scenario
                out = Outcome(error=f"{type(exc).__name__}: {exc}", latency_ms=_ms(t0))
        out.log_lines = lines
        return out

    return run, notes


class _ListHandler(logging.Handler):
    def __init__(self, sink: list[tuple[str, str]]) -> None:
        super().__init__(level=logging.DEBUG)
        self.sink = sink

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.sink.append((record.name, record.getMessage()))
        except Exception:  # noqa: BLE001 — a bad log call must not fail the scenario
            self.sink.append((record.name, str(record.msg)))


@contextmanager
def capture_logs() -> Iterator[list[tuple[str, str]]]:
    """(logger, message) of every record written during one in-process request, INFO too."""
    lines: list[tuple[str, str]] = []
    root = logging.getLogger()
    handler, level = _ListHandler(lines), root.level
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    try:
        yield lines
    finally:
        root.removeHandler(handler)
        root.setLevel(level)


def url_runner(
    base_url: str, no_llm: bool = False, timeout_s: float = 60.0, client: Any = None
) -> tuple[Runner | None, list[str]]:
    """Live API. Probes GET /api/health first: an unreachable server fails fast with one note
    instead of 16 timeouts. `client` is an httpx.Client (tests pass a MockTransport)."""
    import httpx

    http = client or httpx.Client(timeout=timeout_s)
    base = base_url.rstrip("/")
    try:
        health = http.get(f"{base}/api/health")
        if health.status_code == 503:  # server is up but has no data (poi_count 0)
            try:
                info = health.json()
            except ValueError:
                info = {}
            return None, [
                f"API {base} без данных: status={info.get('status')}, "
                f"POI={info.get('poi_count')}, warnings={info.get('warnings', [])}"
            ]
        health.raise_for_status()
        info = health.json()
    except (httpx.HTTPError, ValueError) as exc:
        return None, [f"API {base} недоступен: {type(exc).__name__}"]
    notes = [
        f"API: status={info.get('status')}, POI={info.get('poi_count')}, "
        f"index={info.get('index')}, llm={info.get('llm')}"
    ]
    if str(info.get("llm", "")).startswith("off") and not no_llm:  # "off" / "off (disabled)"
        notes.append("LLM на сервере выключена: ответы шаблонные")

    def run(req: RecommendRequest) -> Outcome:
        # exclude_unset: fields the scenario did not set keep the server's defaults
        body = req.model_dump(mode="json", exclude_none=True, exclude_unset=True)
        t0 = time.perf_counter()
        try:
            resp = http.post(f"{base}/api/recommend", json=body)
        except httpx.HTTPError as exc:
            return Outcome(error=f"сеть: {type(exc).__name__}", latency_ms=_ms(t0))
        ms = _ms(t0)
        if resp.status_code == 422:
            return Outcome(rejected=True, latency_ms=ms, reject_body=resp.text)
        if resp.status_code != 200:
            return Outcome(error=f"HTTP {resp.status_code}", latency_ms=ms)
        try:
            return Outcome(answer=Answer.model_validate(resp.json()), latency_ms=ms)
        except (ValueError, ValidationError) as exc:
            return Outcome(error=f"ответ не по схеме Answer: {str(exc)[:200]}", latency_ms=ms)

    return run, notes


def _ms(t0: float) -> int:
    return int((time.perf_counter() - t0) * 1000)


# ----------------------------------------------------------------------- report


def run_all(
    scenarios: list[dict[str, Any]], runner: Runner, no_llm: bool = False,
    raw_post: RawPostFn | None = None,
) -> list[Result]:
    """raw_post (--url): bodies the schema rejects (expect.reject_required) go to the server
    as is, so the 422 comes from the API; in-process the schema rejection is that 422."""
    outcomes: list[Outcome] = []
    for scn in scenarios:
        data = dict(scn["request"])
        if no_llm:
            data["use_llm"] = False
        try:
            req = RecommendRequest.model_validate(data)
        except ValidationError as exc:
            if (scn.get("expect") or {}).get("reject_required"):
                outcomes.append(_raw_outcome(raw_post, data))
            else:
                outcomes.append(Outcome(error=f"сценарий не по схеме: {exc}"))
            continue
        out = runner(req)
        if "prefs_shift" in (scn.get("expect") or {}) and out.answer is not None:
            base = runner(req.model_copy(update={"prefs": None}))
            out.baseline = base.answer
        outcomes.append(out)
    graph_on = graph_used(outcomes)
    return [grade(scn, out, graph_on) for scn, out in zip(scenarios, outcomes, strict=True)]


def graph_used(outcomes: list[Outcome]) -> bool:
    """Is the walk graph live in this run? The API does not report it, so: at least one answer
    has a graph route. With WALK_GRAPH=false or no graph file, route_source=graph is skipped."""
    return any(
        o.answer is not None and o.answer.route is not None
        and o.answer.route.path_source == "graph"
        for o in outcomes
    )


SEARCH_LABEL = {"vector": "по смыслу", "words": "по словам"}
_SEMANTIC_MARKERS = ("смысл", "мағына", "meaning", "semantic")
_SEARCH_PREFIXES = ("поиск", "іздеу", "search")


def search_line(out: Outcome) -> str | None:
    if out.answer is None:
        return None
    return next(
        (f for f in out.answer.data_used.filters if f.lower().startswith(_SEARCH_PREFIXES)), None
    )


def search_mode(out: Outcome) -> str | None:
    """Did the query get an embedding? In-process mode knows it from the embed_query wrapper;
    over --url it is read from the data_used.filters line «поиск: по смыслу и по словам / по
    словам» (FEATURES). No such line -> None (unknown)."""
    if out.search is not None:
        return out.search
    if out.answer is None:
        return None
    for line in out.answer.data_used.filters:
        low = line.lower()
        if low.startswith(_SEARCH_PREFIXES):
            return "vector" if any(m in low for m in _SEMANTIC_MARKERS) else "words"
    return None


ROUTE_LABEL = {"graph": "по графу", "straight": "прямые"}


def route_summary(results: list[Result]) -> str:
    graph = sum(r.route_source == "graph" for r in results)
    straight = sum(r.route_source == "straight" for r in results)
    none = sum(r.route_source is None for r in results)
    return f"по графу тротуаров {graph}, прямыми отрезками {straight}, без маршрута {none}"


def search_summary(results: list[Result]) -> str:
    vec = sum(r.search == "vector" for r in results)
    words = sum(r.search == "words" for r in results)
    unknown = sum(r.search is None for r in results)
    return f"вектор запроса (embed_query) у {vec}, только по словам {words}, неизвестно {unknown}"


CORE_MEASURE_NOTE = (
    "«основные» — те же 16 запросов, что 16/16 в README и PITCH, но с проверками волны 8 "
    "(факты маршрута 1.3.0, «зебра» только при известной разметке, события). Против сервера "
    "сдачи (контракт 1.2.0) это другая мера, чем 16/16 сдачи: сравнивать только с прогонами "
    "этой же версии eval"
)


def group_summary(results: list[Result]) -> str:
    """«основные 16/16» stays comparable with README, PITCH and the slides; wave 8 apart."""
    core = [r for r in results if r.wave is None]
    w8 = [r for r in results if r.wave == 8]
    parts = [f"основные {sum(r.passed for r in core)}/{len(core)}"]
    if w8:
        parts.append(f"волна 8 {sum(r.passed for r in w8)}/{len(w8)}")
    return ", ".join(parts)


def render_md(mode: str, results: list[Result], notes: list[str]) -> str:
    passed = sum(r.passed for r in results)
    lines = [
        "# Отчёт eval Qadam",
        "",
        f"- Режим: `{mode}`",
        f"- Запуск: {datetime.now().astimezone().strftime('%Y-%m-%d %H:%M %z')}",
        f"- **Итог: {passed}/{len(results)}** ({group_summary(results)})",
        f"- Поиск: {search_summary(results)}",
        f"- Маршруты: {route_summary(results)}",
    ]
    lines += [f"- {n}" for n in notes]
    lines += [
        "",
        "| id | сценарий | итог | что не прошло | карточек | генерация | поиск | маршрут "
        "| latency, мс |",
        "|---|---|---|---|---:|---|---|---|---:|",
    ]
    for r in results:
        why = "; ".join(r.failures).replace("|", "/") or "—"
        lines.append(
            f"| {r.id} | {r.title} | {'pass' if r.passed else 'FAIL'} | {why} | {r.cards} "
            f"| {r.generation or '—'} | {SEARCH_LABEL.get(r.search or '', '—')} "
            f"| {ROUTE_LABEL.get(r.route_source or '', '—')} | {r.latency_ms} |"
        )
    return "\n".join(lines) + "\n"


# ------------------------------------------------- privacy probes (1.2.0, prefs 1.3.0)

_PROBE_LOC = {"landmark_id": "expo"}
# Bodies sent as is (not through RecommendRequest): the server itself must reject them with 422,
# and the 422 body must not repeat what was sent (the 4th item: strings it must not contain).
PRIVACY_PROBES: list[tuple[str, str, dict[str, Any], tuple[str, ...]]] = [
    ("r11", "ячейка H3 r11 (мельче r10)",
     {"query": "кофе рядом", "location": {"h3_cell": "8b2153806d12fff"}}, ("8b2153806d12fff",)),
    ("raw_coords", "сырые координаты lat/lon",
     {"query": "кофе рядом", "location": {"lat": 51.0894, "lon": 71.4161}}, ("51.0894",)),
    ("prefs_unknown", "метка prefs не из словаря",
     {"query": "кофе рядом", "location": _PROBE_LOC, "prefs": {"likes": ["history"]}},
     ("history",)),
    ("prefs_free_text", "свободный текст в prefs",
     {"query": "кофе рядом", "location": _PROBE_LOC,
      "prefs": {"likes": ["тихо, у воды, без людей"]}}, ("у воды",)),
    ("prefs_extra", "лишнее поле в prefs (история)",
     {"query": "кофе рядом", "location": _PROBE_LOC,
      "prefs": {"likes": ["quiet"], "history": ["coffee"]}}, ("history",)),
    # 1.3.1: separate likes / avoids vocabularies, contradicting pairs
    ("prefs_conflict_kids", "противоречие: с ребёнком + не хочу детские места",
     {"query": "кофе рядом", "location": _PROBE_LOC,
      "prefs": {"likes": ["kids"], "avoids": ["kids_places"]}}, ("kids_places",)),
    ("prefs_conflict_coffee", "противоречие: кофе + не хочу кафе",
     {"query": "кофе рядом", "location": _PROBE_LOC,
      "prefs": {"likes": ["coffee"], "avoids": ["cafes"]}}, ("cafes",)),
    # the vocabularies do not overlap, so «one tag in both lists» is this same case
    ("prefs_foreign", "метка из чужого списка (музеи в «не хочу»)",
     {"query": "кофе рядом", "location": _PROBE_LOC,
      "prefs": {"avoids": ["museums"]}}, ("museums",)),
    ("prefs_no_bars_avoid", "no_bars в «не хочу»",
     {"query": "кофе рядом", "location": _PROBE_LOC,
      "prefs": {"avoids": ["no_bars"]}}, ("no_bars",)),
]

# 1.3.1 (Megah, 21.10): the 1.3.0 like «no_bars» is a synonym of the avoid «bars», not a 422.
# Sent through the scenario runner: it must be answered and say «избегать: бары» in filters.
COMPAT_NO_BARS = {"query": "Хочу погулять рядом", "location": {"landmark_id": "expo"},
                  "at": "2026-10-20T15:00:00+05:00", "lang": "ru",
                  "prefs": {"likes": ["no_bars"]}}
AVOID_BARS_MARKERS = ("избегать:", "бары")


def run_compat_probe(runner: Runner | None, no_llm: bool = False) -> dict[str, Any]:
    """likes [no_bars] -> an answer (not 422), filters «избегать: бары», no bar cards."""
    title = "likes no_bars (1.3.0) → синоним «не хочу бары»"
    if runner is None:
        return {"id": "compat_no_bars", "title": title, "got": "раннер недоступен",
                "passed": False}
    data = dict(COMPAT_NO_BARS, use_llm=not no_llm)
    try:
        req = RecommendRequest.model_validate(data)
    except ValidationError:
        return {"id": "compat_no_bars", "title": title, "got": "422 (схема)", "passed": False}
    out = runner(req)
    if out.answer is None:
        got = "422" if out.rejected else (out.error or "нет ответа")
        return {"id": "compat_no_bars", "title": title, "got": got, "passed": False}
    lines = [f.lower() for f in out.answer.data_used.filters]
    said = any(all(m in f for m in AVOID_BARS_MARKERS) for f in lines)
    bars = [c.title for c in out.answer.cards if c.category in ("bar", "nightlife")]
    problems = ([] if said else ["в filters нет «избегать: бары»"]) + (
        [f"бары в карточках: {bars}"] if bars else [])
    return {"id": "compat_no_bars", "title": title,
            "got": "ответ" + (f" ({'; '.join(problems)})" if problems else ", «избегать: бары»"),
            "passed": not problems}
# sends one body; returns the HTTP status (-1 = no answer) or (status, response text)
PostFn = Callable[[dict[str, Any]], "int | tuple[int, str]"]
RawPostFn = PostFn


def _status_text(got: int | tuple[int, str]) -> tuple[int, str | None]:
    return got if isinstance(got, tuple) else (got, None)


def _raw_outcome(post: RawPostFn | None, body: dict[str, Any]) -> Outcome:
    """A body the schema rejects: in-process that is the 422; with --url ask the server."""
    if post is None:
        return Outcome(rejected=True)
    t0 = time.perf_counter()
    status, text = _status_text(post(body))
    if status == 422:
        return Outcome(rejected=True, latency_ms=_ms(t0), reject_body=text)
    return Outcome(error=f"HTTP {status}" if status >= 0 else "нет ответа", latency_ms=_ms(t0))


def run_privacy_probes(post: PostFn | None) -> list[dict[str, Any]]:
    """Each probe must be rejected with 422. post=None: check the request schema in-process
    (the API validates with the same model, so a schema rejection is a 422 there)."""
    out = []
    for pid, title, body, secrets in PRIVACY_PROBES:
        echoed: list[str] = []
        if post is None:
            try:
                RecommendRequest.model_validate(body)
                got = "принят схемой"
            except ValidationError:
                got = "422"
        else:
            status, text = _status_text(post(body))
            got = "нет ответа" if status < 0 else str(status)
            echoed = [s for s in secrets if text is not None and s.lower() in text.lower()]
            if echoed:
                got += " (ответ повторяет ввод)"
        out.append({"id": pid, "title": title, "got": got,
                     "passed": got == "422" and not echoed})
    return out


def url_post(base_url: str, timeout_s: float = 30.0) -> PostFn:
    import httpx

    endpoint = base_url.rstrip("/") + "/api/recommend"

    def post(body: dict[str, Any]) -> tuple[int, str]:
        try:
            resp = httpx.post(endpoint, json=body, timeout=timeout_s)
            return resp.status_code, resp.text
        except httpx.HTTPError:
            return -1, ""

    return post


def privacy_summary(probes: list[dict[str, Any]]) -> str:
    ok = sum(p["passed"] for p in probes)
    parts = "; ".join(f"{p['title']} → {p['got']}" for p in probes)
    return f"{ok}/{len(probes)} отклонены с 422 ({parts})"


def write_report(
    out_dir: Path, mode: str, results: list[Result], notes: list[str],
    privacy: list[dict[str, Any]] | None = None, compat: dict[str, Any] | None = None,
) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    if privacy is not None:
        notes = [*notes, f"Приватность: {privacy_summary(privacy)}"]
    (out_dir / "report.md").write_text(render_md(mode, results, notes), encoding="utf-8")
    payload = {
        "mode": mode,
        "passed": sum(r.passed for r in results),
        "total": len(results),
        "groups": {
            "core": {"passed": sum(r.passed for r in results if r.wave is None),
                     "total": sum(r.wave is None for r in results)},
            "wave8": {"passed": sum(r.passed for r in results if r.wave == 8),
                      "total": sum(r.wave == 8 for r in results)},
        },
        "notes": notes,
        "search": {k: sum(r.search == k for r in results) for k in ("vector", "words")},
        "routes": {k: sum(r.route_source == k for r in results) for k in ("graph", "straight")},
        "privacy": privacy or [],
        "compat": compat,
        "results": [asdict(r) for r in results],
    }
    (out_dir / "report.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )


# ------------------------------------------------------------------------ smoke

QUERIES_PATH = Path(__file__).resolve().parent / "queries_team.csv"
SMOKE_DEFAULT_LOCATION = "expo"
SMOKE_DEFAULT_AT = "2026-10-19T15:00:00+05:00"  # Monday, same day as scenarios.yaml
_H3_CELL = re.compile(r"^[0-9a-f]{15}$")


@dataclass
class SmokeItem:
    id: str
    query: str
    location: str
    at: str
    outcome: Outcome
    remarks: list[str] = field(default_factory=list)  # common invariants, informational only


def load_queries(path: Path = QUERIES_PATH) -> list[dict[str, str]]:
    """CSV with header id,query[,location,at,lang]. Blank and '#' lines are skipped.

    location: landmark id (expo, baiterek, ...) or an H3 cell; empty -> SMOKE_DEFAULT_LOCATION.
    """
    with path.open(encoding="utf-8-sig", newline="") as fh:
        lines = [ln for ln in fh if ln.strip() and not ln.lstrip().startswith("#")]
    rows: list[dict[str, str]] = []
    for n, raw in enumerate(csv.DictReader(lines), start=1):
        row = {k.strip(): (v or "").strip() for k, v in raw.items() if isinstance(k, str)}
        if not row.get("query"):
            continue
        row["id"] = row.get("id") or f"Q{n:02d}"
        rows.append(row)
    return rows


def smoke_request(row: dict[str, str], no_llm: bool) -> RecommendRequest:
    loc = row.get("location") or SMOKE_DEFAULT_LOCATION
    data: dict[str, Any] = {
        "query": row["query"],
        "location": {"h3_cell": loc} if _H3_CELL.match(loc) else {"landmark_id": loc},
        "at": row.get("at") or SMOKE_DEFAULT_AT,
    }
    if row.get("lang"):
        data["lang"] = row["lang"]
    if no_llm:
        data["use_llm"] = False
    return RecommendRequest.model_validate(data)


def run_smoke(
    rows: list[dict[str, str]], runner: Runner | None, no_llm: bool, setup_error: str
) -> list[SmokeItem]:
    items = []
    for row in rows:
        try:
            req = smoke_request(row, no_llm)
        except ValidationError as exc:
            first = exc.errors()[0]
            where = ".".join(str(p) for p in first.get("loc", ()))
            out = Outcome(error=f"запрос не по схеме: {where}: {first.get('msg', '')}")
        else:
            out = runner(req) if runner is not None else Outcome(error=setup_error)
        items.append(
            SmokeItem(
                id=row["id"],
                query=row["query"],
                location=row.get("location") or SMOKE_DEFAULT_LOCATION,
                at=row.get("at") or SMOKE_DEFAULT_AT,
                outcome=out,
                remarks=check_invariants(out.answer) if out.answer else [],
            )
        )
    return items


def _cell(text: str, limit: int = 60) -> str:
    text = " ".join(str(text).split()).replace("|", "/")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _intent_line(ans: Answer) -> str:
    it = ans.intent
    flags = [
        name
        for name, on in (
            ("с ребёнком", it.with_kids),
            ("коляска", it.wheelchair),
            ("ночь", it.night),
            ("турист", it.tourist),
        )
        if on
    ]
    parts = [
        f"parser={it.parser}",
        f"lang={it.lang}",
        f"бюджет={it.time_budget_min or '—'} мин",
        f"пешком ≤ {it.max_walk_min or '—'} мин",
        f"категории={', '.join(it.categories) or '—'}",
        f"теги={', '.join(it.must_tags) or '—'}",
    ]
    if it.indoor != "any":
        parts.append(f"indoor={it.indoor}")
    if it.group_size:
        parts.append(f"группа={it.group_size}")
    if flags:
        parts.append("флаги: " + ", ".join(flags))
    return "; ".join(parts)


def _render_answer(item: SmokeItem) -> list[str]:
    out = item.outcome
    if out.error:
        return [f"- **Ошибка:** {out.error}"]
    if out.rejected:
        return ["- **Отклонён (422):** неверный ввод, ответа нет"]
    ans = out.answer
    assert ans is not None
    lines = [
        f"- Разбор: {_intent_line(ans)}",
        f"- Ответ ({ans.data_used.generation}, {out.latency_ms} мс): {ans.summary or '—'}",
    ]
    if not ans.cards:
        lines.append("- Карточек нет")
    for c in ans.cards:
        lines += [
            f"{c.rank}. **{c.title}**: {c.category}, {c.distance_m} м, ≈{c.walk_min} мин пешком, "
            f"на месте {c.visit_min_low}–{c.visit_min_high} мин",
            f"   - Почему: {c.why}",
            f"   - Что делать: {c.what_to_do}",
            f"   - Часы: {c.open_status}/{c.open_confidence}, {c.open_note} · идея: {c.idea_source}"
            f" · факты: {', '.join(c.facts_sources)}",
        ]
    if ans.route is not None:
        titles = {c.poi_id: c.title for c in ans.cards}
        stops = " → ".join(titles.get(s, s) for s in ans.route.stop_ids)
        fits = "да" if ans.route.fits_budget else "нет"
        lines.append(
            f"- Маршрут: {stops} · {ans.route.total_min} мин · в бюджете: {fits} · "
            f"{ans.route.note}"
        )
    if ans.warnings:
        lines.append("- Предупреждения: " + "; ".join(ans.warnings))
    if item.remarks:
        lines.append("- Замечания (инварианты eval): " + "; ".join(item.remarks))
    return lines


def render_smoke_md(mode: str, items: list[SmokeItem], notes: list[str], queries: Path) -> str:
    answered = [i for i in items if i.outcome.answer is not None]
    rejected = sum(i.outcome.rejected for i in items)
    errors = sum(i.outcome.error is not None for i in items)
    with_cards = sum(bool(i.outcome.answer.cards) for i in answered if i.outcome.answer)
    gens: dict[str, int] = {}
    for i in answered:
        assert i.outcome.answer is not None
        gen = i.outcome.answer.data_used.generation
        gens[gen] = gens.get(gen, 0) + 1
    latencies = [i.outcome.latency_ms for i in answered]
    lines = [
        "# Smoke-прогон запросов команды",
        "",
        "Строгих проверок нет: отчёт для просмотра глазами. «Замечания» — общие инварианты eval,",
        "они подсказывают, куда смотреть, но не означают провал.",
        "",
        f"- Режим: `{mode}`",
        f"- Запуск: {datetime.now().astimezone().strftime('%Y-%m-%d %H:%M %z')}",
        f"- Запросы: `{queries.name}`",
        f"- **Ответили: {len(answered)}/{len(items)}**; отклонено (422): {rejected}; "
        f"ошибок: {errors}; с карточками: {with_cards}",
        "- Генерация: " + (", ".join(f"{k} {v}" for k, v in sorted(gens.items())) or "—"),
    ]
    if latencies:
        lines.append(
            f"- Время ответа: медиана {int(statistics.median(latencies))} мс, "
            f"макс {max(latencies)} мс"
        )
    lines += [f"- {n}" for n in notes]
    lines += [
        "",
        "## Сводка",
        "",
        "| id | запрос | место | карточек | первая карточка | генерация | мс | замечания |",
        "|---|---|---|---:|---|---|---:|---:|",
    ]
    for i in items:
        ans = i.outcome.answer
        if ans is not None:
            first = ans.cards[0].title if ans.cards else "—"
            row = (len(ans.cards), _cell(first, 40), ans.data_used.generation)
        else:
            row = (0, "422" if i.outcome.rejected else _cell(f"ошибка: {i.outcome.error}", 40), "—")
        lines.append(
            f"| {i.id} | {_cell(i.query)} | {_cell(i.location, 20)} | {row[0]} | {row[1]} "
            f"| {row[2]} | {i.outcome.latency_ms} | {len(i.remarks)} |"
        )
    lines += ["", "## Ответы"]
    for i in items:
        lines += ["", f"### {i.id} · {i.query}", "", f"- Место: `{i.location}` · время: {i.at}"]
        lines += _render_answer(i)
    return "\n".join(lines) + "\n"


def write_smoke_report(
    out_dir: Path, mode: str, items: list[SmokeItem], notes: list[str], queries: Path
) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "smoke_report.md"
    path.write_text(render_smoke_md(mode, items, notes, queries), encoding="utf-8")
    return path


# ------------------------------------------------------------------------- main


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Qadam scenario eval")
    group = ap.add_mutually_exclusive_group()
    group.add_argument("--mock", action="store_true", help="use web/mock/answer_sample.json")
    group.add_argument("--url", help="live API base URL, e.g. http://localhost:8000")
    ap.add_argument("--no-llm", action="store_true", help="send use_llm=false in every request")
    ap.add_argument("--only", help="comma-separated scenario ids, e.g. S1,E3")
    ap.add_argument("--out", type=Path, default=Path(__file__).resolve().parent)
    ap.add_argument("--scenarios", type=Path, default=SCENARIOS_PATH)
    ap.add_argument("--smoke", action="store_true", help="run team queries, no grading")
    ap.add_argument("--queries", type=Path, default=QUERIES_PATH, help="CSV for --smoke")
    ap.add_argument("--note", action="append", default=[], help="extra line for the report header")
    ap.add_argument("--local-data-check", "--graph-check", dest="local_data_check",
                    action="store_true",
                    help="with --url: check crossings and event venues against the local "
                         "walk graph and data/events.json too")
    args = ap.parse_args(argv)

    notes: list[str] = []
    runner: Runner | None
    if args.mock:
        mode = "mock"
        runner = mock_runner()
        notes.append("МОК: один и тот же ответ на все запросы — проверяет сам eval, не систему")
    elif args.url:
        mode = f"url {args.url}" + (" --no-llm" if args.no_llm else "")
        runner, notes = url_runner(args.url, no_llm=args.no_llm)
    else:
        mode = "in-process" + (" --no-llm" if args.no_llm else "")
        runner, notes = inprocess_runner(args.no_llm)
    notes = notes + args.note
    global CROSSING_GRAPH, EVENTS_FILE
    local = not args.url or args.local_data_check
    CROSSING_GRAPH = GRAPH_PATH if local else None
    EVENTS_FILE = EVENTS_PATH if local else None

    if args.smoke:
        if not args.queries.is_file():
            print(f"SMOKE: нет файла запросов {args.queries}. Формат: заголовок "
                  "id,query,location,at,lang; пример — eval/queries_team.csv")
            return 0
        rows = load_queries(args.queries)
        setup_error = notes[0] if runner is None and notes else "раннер недоступен"
        items = run_smoke(rows, runner, args.no_llm, setup_error)
        path = write_smoke_report(args.out, mode, items, notes, args.queries)
        answered = sum(i.outcome.answer is not None for i in items)
        for n in notes:
            print(f"note: {n}")
        print(f"SMOKE {mode}: ответили {answered}/{len(items)} -> {path}")
        return 0

    scenarios = load_scenarios(args.scenarios)
    if args.only:
        wanted = {s.strip() for s in args.only.split(",")}
        scenarios = [s for s in scenarios if s["id"] in wanted]

    if runner is None:
        results = [grade(s, Outcome(error=notes[0])) for s in scenarios]
    else:
        raw_post = url_post(args.url) if args.url else None
        results = run_all(scenarios, runner, no_llm=args.no_llm, raw_post=raw_post)

    wants_graph = [
        s["id"] for s in scenarios if (s.get("expect") or {}).get("route_source") == "graph"
    ]
    if wants_graph and not any(r.route_source == "graph" for r in results):
        notes.append("граф тротуаров в этом прогоне не используется (WALK_GRAPH=false или "
                     "нет файла графа): ожидание route_source=graph пропущено для "
                     + ", ".join(wants_graph))
    for note in (crossing_check_note(bool(args.url)), events_check_note(bool(args.url))):
        if note:
            notes.append(note)
    notes.append(CORE_MEASURE_NOTE)
    privacy = run_privacy_probes(url_post(args.url) if args.url else None)
    compat = run_compat_probe(runner, no_llm=args.no_llm)
    notes.append(f"Совместимость: {compat['title']} — {compat['got']}"
                 f" ({'pass' if compat['passed'] else 'FAIL'})")
    write_report(args.out, mode, results, notes, privacy, compat)
    passed = sum(r.passed for r in results)
    for r in results:
        print(f"{r.id:<4} {'pass' if r.passed else 'FAIL'}  {'; '.join(r.failures)[:160]}")
    for n in notes:
        print(f"note: {n}")
    print(f"PRIVACY: {privacy_summary(privacy)}")
    print(f"COMPAT: {compat['got']} ({'pass' if compat['passed'] else 'FAIL'})")
    print(f"EVAL {mode}: {passed}/{len(results)} ({group_summary(results)}) "
          f"-> {args.out / 'report.md'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
