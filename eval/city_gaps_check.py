"""Checks of the city gaps map (wave 9, T144): data/processed/city_gaps.json and GET /api/city-gaps.

Usage:
  python -m eval.city_gaps_check                       # in-process: the app on a test client
  python -m eval.city_gaps_check --file web/mock/city_gaps.json
  python -m eval.city_gaps_check --url http://localhost:8010 [--local-data-check]

1. The answer passes CityGaps (1.4.0); attribution and licence are in place; cells are valid H3 r8;
   city is the sum over cells (checked here again, not only by the schema).
2. The city totals agree with the DATA report (ARCHITECTURE §5, «Сверка с отчётом DATA»):
   walk_coverage.json and poi.jsonl exactly, length to 0.5 km, shares to 0.1 p.p., and the same
   numbers in REPORT.md. With --url the local files may differ from the server's: off unless
   --local-data-check.
3. Privacy: the endpoint takes no user data. A request with a cell, coordinates and a query text
   in its parameters gets the same answer as a plain one, none of those values is in the answer,
   and (in-process) none is in the log.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from unittest import mock
from urllib.parse import quote, quote_plus

from pydantic import ValidationError

from app.schemas import (
    GAP_H3_RES,
    GAP_MAX_OUTLINE_POINTS,
    ODBL_URL,
    OSM_ATTRIBUTION,
    OSM_COPYRIGHT_URL,
    CityGaps,
    GapCounts,
)

ROOT = Path(__file__).resolve().parent.parent
PROCESSED = ROOT / "data" / "processed"
LENGTH_TOL_KM = 0.5
SHARE_TOL_PP = 0.1
# What a curious client might send: a cell, raw coordinates, a query, prefs. All must be ignored.
PROBE_PARAMS = {"h3_cell": "8a2153806d1ffff", "lat": "51.0894", "lon": "71.4161",
                "query": "кофе рядом с домом", "prefs": "quiet"}

# (params, path) -> status, text, lowercased headers; path defaults to /api/city-gaps
GetFn = Callable[..., tuple[int, str, dict[str, str]]]
CITY_GAPS_PATH = "/api/city-gaps"
HEALTH_PATH = "/api/health"
UNAVAILABLE = {"detail": "city gaps unavailable"}  # the whole 503 body, ARCHITECTURE §5


@dataclass
class Check:
    id: str
    title: str
    passed: bool
    detail: str = ""


# ------------------------------------------------------------------------- shape


def check_shape(raw: Any) -> tuple[CityGaps | None, list[Check]]:
    try:
        gaps = CityGaps.model_validate(raw)
    except ValidationError as exc:
        return None, [Check("schema", "ответ проходит схему CityGaps", False,
                            f"{exc.error_count()} ошибок схемы")]
    checks = [Check("schema", "ответ проходит схему CityGaps", True, f"ячеек {len(gaps.cells)}")]
    attrib = (gaps.attribution == OSM_ATTRIBUTION and gaps.license == "ODbL-1.0"
              and gaps.license_url == ODBL_URL and gaps.copyright_url == OSM_COPYRIGHT_URL)
    checks.append(Check("attribution", "атрибуция OSM и лицензия ODbL на месте", attrib,
                        f"{gaps.attribution}, {gaps.license}; данные: места {gaps.poi_date}, "
                        f"граф {gaps.walk_graph_date}"))
    import h3

    bad = [c.h3_cell for c in gaps.cells
           if not h3.is_valid_cell(c.h3_cell) or h3.get_resolution(c.h3_cell) != GAP_H3_RES]
    checks.append(Check("cells_r8", f"все ячейки — H3 r{GAP_H3_RES}", not bad,
                        f"не r{GAP_H3_RES}: {bad[:3]}" if bad else f"{len(gaps.cells)} ячеек"))
    off = [name for name in GapCounts.model_fields
           if getattr(gaps.city, name) != sum(getattr(c, name) for c in gaps.cells)]
    checks.append(Check("city_sum", "итоги city — сумма по ячейкам", not off,
                        f"не сходится: {off}" if off else "все 9 счётчиков"))
    return gaps, checks


# ------------------------------------------------------------ districts (1.4.1)


def _sum_counts(items: list[GapCounts]) -> dict[str, int]:
    return {name: sum(getattr(c, name) for c in items) for name in GapCounts.model_fields}


def district_sums(gaps: CityGaps) -> tuple[dict[str, int], int]:
    """Districts plus «вне районов»: the nine counters and the number of cells."""
    blk = gaps.districts
    assert blk is not None
    parts = [d.counts for d in blk.items if d.counts is not None] + [blk.unassigned]
    return _sum_counts(parts), sum(d.cell_count for d in blk.items) + blk.unassigned_cells


def _in_ring(lat: float, lon: float, ring: list[Any]) -> bool:
    """Even-odd ray casting over one closed ring of (lat, lon)."""
    inside = False
    for (a_lat, a_lon), (b_lat, b_lon) in zip(ring, ring[1:], strict=False):
        if (a_lat > lat) != (b_lat > lat):
            cross = a_lon + (lat - a_lat) * (b_lon - a_lon) / (b_lat - a_lat)
            if lon < cross:
                inside = not inside
    return inside


def _in_outline(lat: float, lon: float, outline: list[Any]) -> bool:
    return sum(_in_ring(lat, lon, ring) for ring in outline) % 2 == 1


def check_districts(gaps: CityGaps) -> list[Check]:
    """1.4.1: statuses, outlines, the official list, sums (districts + unassigned = city), and
    every cell where its centre is (an independent even-odd test, not the pipeline's)."""
    blk = gaps.districts
    if blk is None:
        return [Check("districts", "блок районов есть (1.4.1)", False,
                      "файл 1.4.0 без районов (районы — DATA T151)")]
    checks: list[Check] = []
    city = gaps.city.model_dump()
    sums, cells = district_sums(gaps)
    off = [k for k in city if sums[k] != city[k]]
    checks.append(Check("districts_sum", "районы + «вне районов» = город (9 счётчиков)", not off,
                        f"не сходится: {off}" if off else f"районов {len(blk.items)}"))
    checks.append(Check("districts_cells", "ячейки районов + «вне районов» = все ячейки",
                        cells == len(gaps.cells), f"{cells} из {len(gaps.cells)}"))
    bad_status: list[str] = []
    for d in blk.items:
        has = (d.osm_relation is not None, d.boundary_date is not None,
               d.counts is not None, bool(d.outline))
        if d.status == "missing" and (any(has) or d.cell_count):
            bad_status.append(f"{d.id}: missing с числами или контуром")
        if d.status != "missing" and not all(has):
            bad_status.append(f"{d.id}: {d.status} без границы, даты, чисел или контура")
    by_status = {s: sum(d.status == s for d in blk.items) for s in ("ok", "stale", "missing")}
    checks.append(Check("districts_status", "у missing нет чисел и контура, у ok/stale есть",
                        not bad_status, "; ".join(bad_status) or
                        f"ok {by_status['ok']}, stale {by_status['stale']}, "
                        f"missing {by_status['missing']}"))
    open_rings = [d.id for d in blk.items for ring in d.outline
                  if len(ring) < 4 or list(ring[0]) != list(ring[-1])]
    too_many = [d.id for d in blk.items
                if sum(len(r) for r in d.outline) > GAP_MAX_OUTLINE_POINTS]
    checks.append(Check("districts_outline", "контуры замкнуты, точек не больше предела",
                        not open_rings and not too_many,
                        f"незамкнутые: {open_rings}; сверх предела: {too_many}"
                        if open_rings or too_many else
                        f"точек всего {sum(len(r) for d in blk.items for r in d.outline)}"))
    listed = blk.official_list_url, blk.official_list_checked
    list_ok = (listed == (None, None)) or (
        bool(listed[0]) and str(listed[0]).startswith("https://") and listed[1] is not None)
    checks.append(Check("districts_official", "официальный список: https и дата или оба пусто",
                        list_ok, "список не сверен (оба пусто)" if listed == (None, None)
                        else f"{listed[0]} от {listed[1]}"))
    import h3

    ids = {d.id: d for d in blk.items}
    wrong: list[str] = []
    for c in gaps.cells:
        lat, lon = h3.cell_to_latlng(c.h3_cell)
        if c.district is not None:
            d = ids.get(c.district)
            if d is None or d.status == "missing" or not _in_outline(lat, lon, d.outline):
                wrong.append(f"{c.h3_cell}→{c.district}")
        elif any(d.outline and _in_outline(lat, lon, d.outline) for d in blk.items):
            wrong.append(f"{c.h3_cell}→вне районов")
    per = {d.id: [c for c in gaps.cells if c.district == d.id] for d in blk.items}
    mism = [d.id for d in blk.items if d.status != "missing" and (
        len(per[d.id]) != d.cell_count
        or _sum_counts(per[d.id]) != (d.counts.model_dump() if d.counts else None))]
    free = [c for c in gaps.cells if c.district is None]
    if len(free) != blk.unassigned_cells or _sum_counts(free) != blk.unassigned.model_dump():
        mism.append("вне районов")
    checks.append(Check("districts_cells_inside", "ячейка — в районе своего центра, числа "
                        "района — сумма его ячеек", not wrong and not mism,
                        f"не там: {wrong[:3]}; не сходятся: {mism}" if wrong or mism
                        else f"{len(gaps.cells)} ячеек"))
    return checks


# --------------------------------------------------------- reconciliation (DATA)


@dataclass
class LocalTotals:
    """City totals from the DATA files, the way ARCHITECTURE §5 defines them."""

    poi: int
    poi_no_hours: int
    poi_wheelchair_unknown: int
    poi_no_name_kk: int
    length_km: float
    lit_unknown_share: float  # of length, 0..1
    crossings: int
    crossings_kind_unknown: int
    crossings_kerb_unknown: int
    report: dict[str, float]  # the same numbers as REPORT.md prints them


def _report_numbers(text: str) -> dict[str, float]:
    """Numbers REPORT.md prints: lit unknown %, crossings without type, kerb unknown, wheelchair."""
    found: dict[str, float] = {}
    patterns = {
        "lit_unknown_pct": r"^\| нет данных об освещении \| [\d.]+ \| ([\d.]+)% \|",
        "crossings_kind_unknown": r"^\| тип не указан \| (\d+) \|",
        "crossings_kerb_unknown": r"^\| бордюр: нет данных \| (\d+) \|",
        "poi_wheelchair_unknown": r"^\| unknown \| (\d+) \|",
    }
    for key, pattern in patterns.items():
        match = re.search(pattern, text, flags=re.MULTILINE)
        if match:
            found[key] = float(match.group(1))
    return found


def local_totals(processed: Path = PROCESSED) -> LocalTotals:
    cov = json.loads((processed / "walk_coverage.json").read_text(encoding="utf-8"))
    pois = [json.loads(line) for line in
            (processed / "poi.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    report = (processed / "REPORT.md").read_text(encoding="utf-8")
    return LocalTotals(
        poi=len(pois),
        poi_no_hours=sum(not (p.get("opening_hours") or "").strip() for p in pois),
        poi_wheelchair_unknown=sum(p.get("wheelchair", "unknown") == "unknown" for p in pois),
        poi_no_name_kk=sum(not ((p.get("name_i18n") or {}).get("kk") or "").strip()
                           for p in pois),
        length_km=float(cov["length_km"]),
        lit_unknown_share=float(cov["lit"]["unknown"]["length_share"]),
        crossings=int(cov["crossings_total"]),
        crossings_kind_unknown=int(cov["crossings"]["unknown"]["nodes"]),
        crossings_kerb_unknown=int(cov["kerbs"]["unknown"]["nodes"]),
        report=_report_numbers(report),
    )


def check_reconcile(gaps: CityGaps, local: LocalTotals) -> list[Check]:
    c = gaps.city
    checks: list[Check] = []

    def exact(cid: str, title: str, got: int, want: int, source: str) -> None:
        checks.append(Check(cid, title, got == want, f"city {got}, {source} {want}"))

    exact("poi", "мест = строк poi.jsonl", c.poi, local.poi, "poi.jsonl")
    exact("poi_wheelchair_unknown", "коляска «нет данных» = wheelchair unknown в poi.jsonl",
          c.poi_wheelchair_unknown, local.poi_wheelchair_unknown, "poi.jsonl")
    exact("poi_no_hours", "без часов = мест без opening_hours в poi.jsonl",
          c.poi_no_hours, local.poi_no_hours, "poi.jsonl")
    exact("poi_no_name_kk", "без казахского названия = мест без name_i18n.kk в poi.jsonl",
          c.poi_no_name_kk, local.poi_no_name_kk, "poi.jsonl")
    exact("crossings", "переходов = crossings_total", c.crossings, local.crossings,
          "walk_coverage")
    exact("crossings_kind_unknown", "тип перехода неизвестен = crossings.unknown.nodes",
          c.crossings_kind_unknown, local.crossings_kind_unknown, "walk_coverage")
    exact("crossings_kerb_unknown", "бордюр неизвестен = kerbs.unknown.nodes",
          c.crossings_kerb_unknown, local.crossings_kerb_unknown, "walk_coverage")
    km = c.walk_m / 1000
    checks.append(Check("walk_km", f"длина графа: до {LENGTH_TOL_KM} км",
                        abs(km - local.length_km) <= LENGTH_TOL_KM,
                        f"city {km:.1f} км, walk_coverage {local.length_km} км"))
    share = 100 * c.walk_lit_unknown_m / c.walk_m if c.walk_m else 0.0
    want = 100 * local.lit_unknown_share
    checks.append(Check("lit_unknown_share", f"освещённость «нет данных»: до {SHARE_TOL_PP} п. п.",
                        abs(share - want) <= SHARE_TOL_PP + 1e-9,
                        f"city {share:.2f} %, walk_coverage {want:.2f} %"))
    # the same numbers in REPORT.md, the way the report prints them
    rep = local.report
    pairs = [("report_lit", "REPORT.md: освещённость «нет данных», %", "lit_unknown_pct", share),
             ("report_kind", "REPORT.md: тип перехода не указан", "crossings_kind_unknown",
              c.crossings_kind_unknown),
             ("report_kerb", "REPORT.md: бордюр — нет данных", "crossings_kerb_unknown",
              c.crossings_kerb_unknown),
             ("report_wheelchair", "REPORT.md: wheelchair unknown", "poi_wheelchair_unknown",
              c.poi_wheelchair_unknown)]
    for cid, title, key, got in pairs:
        if key not in rep:
            checks.append(Check(cid, title, False, "строки нет в REPORT.md"))
            continue
        tol = SHARE_TOL_PP if key == "lit_unknown_pct" else 0
        # REPORT prints the share rounded to 0.1 %: allow that rounding on top
        tol = tol + 0.05 if key == "lit_unknown_pct" else tol
        checks.append(Check(cid, title, abs(got - rep[key]) <= tol + 1e-9,
                            f"city {got:.1f}, REPORT.md {rep[key]:g}"))
    return checks


# ------------------------------------------------------------------- privacy


def check_privacy(get: GetFn, log_lines: Callable[[], list[str]] | None = None) -> list[Check]:
    """Parameters with a cell, coordinates and a text change nothing and leak nowhere."""
    status0, text0, headers0 = get({})
    with_log = log_lines is not None
    status1, text1, _ = get(dict(PROBE_PARAMS))
    lines = log_lines() if log_lines is not None else []
    title = "параметры с ячейкой, координатами и текстом игнорируются"
    if status0 < 0 or status1 < 0:
        return [Check("privacy_params", title, False, "сервер не ответил")]
    if status0 == 503 or status1 == 503:
        return [Check("privacy_params", title, False, "503: файла карты нет, проверить нечем")]
    if status0 != 200 or status1 != 200:
        return [Check("privacy_params", title, False, f"HTTP {status0} / {status1}")]
    same = _json(text0) == _json(text1)
    values = probe_forms()
    # an echo is a value that the parameters brought in: one that is in the plain answer too
    # (e.g. «51.0894» inside an outline point 51.08941) is part of the map, not of the request
    in_answer = sorted({v for v in values if v in text1 and v not in text0})
    checks = [
        Check("privacy_params", title, same and not in_answer,
              "ответ тот же, значений параметров в нём нет" if same and not in_answer else
              f"ответ {'тот же' if same else 'другой'}; в ответе: {in_answer}"),
        Check("cache_control", "Cache-Control: public, max-age=3600",
              headers0.get("cache-control", "").replace(" ", "") == "public,max-age=3600",
              headers0.get("cache-control", "нет заголовка")),
    ]
    if with_log:
        # count only: a log line may hold what was sent, it is never copied into the report
        leaked = sum(any(v in line for v in values) for line in lines)  # encoded forms too
        checks.append(Check("privacy_log", "значений параметров нет в логе", leaked == 0,
                            f"записей лога {len(lines)}, с параметрами {leaked}"))
    return checks


def probe_forms() -> list[str]:
    """Each probe value as sent and as a URL carries it (percent-encoded, «+» for spaces)."""
    forms: list[str] = []
    for value in PROBE_PARAMS.values():
        for form in (value, quote(value, safe=""), quote_plus(value)):
            if form not in forms:
                forms.append(form)
    return forms


def check_health(get: GetFn) -> list[Check]:
    """/api/health names the map state: ready when the map answers 200, missing on 503;
    a 503 body is exactly {"detail": "city gaps unavailable"}."""
    status, text, _ = get({})
    h_status, h_text, _ = get({}, path=HEALTH_PATH)
    health = _json(h_text) if h_status in (200, 503) else None
    want = {200: "ready", 503: "missing"}.get(status)
    got = health.get("city_gaps") if isinstance(health, dict) else None
    checks = [Check("health_field", "/api/health: city_gaps ready | missing по ответу карты",
                    want is not None and got == want,
                    f"карта HTTP {status}, health.city_gaps={got!r}")]
    if status == 503:
        checks.append(Check("unavailable_body", "503: тело ровно {\"detail\": \"city gaps "
                            "unavailable\"}", _json(text) == UNAVAILABLE, text[:80]))
    return checks


def check_missing_file(get_with: GetFn, get_without: GetFn) -> list[Check]:
    """The same app without the map file: 503 with the fixed body, health «missing», and the
    overall health status the same as with the file (the rest of the API keeps working)."""
    status, text, _ = get_without({})
    h_with = _json(get_with({}, path=HEALTH_PATH)[1]) or {}
    h_without = _json(get_without({}, path=HEALTH_PATH)[1]) or {}
    return [
        Check("missing_503", "без файла: 503 {\"detail\": \"city gaps unavailable\"}",
              status == 503 and _json(text) == UNAVAILABLE, f"HTTP {status}: {text[:60]}"),
        Check("missing_health", "без файла: health.city_gaps = missing, status тот же",
              h_without.get("city_gaps") == "missing"
              and h_without.get("status") == h_with.get("status"),
              f"city_gaps={h_without.get('city_gaps')!r}, status {h_with.get('status')!r} → "
              f"{h_without.get('status')!r}"),
    ]


def _json(text: str) -> Any:
    try:
        return json.loads(text)
    except ValueError:
        return None


# ------------------------------------------------------------------- sources


@contextmanager
def city_gaps_file(path: Path | None) -> Iterator[None]:
    """Point QADAM_CITY_GAPS_PATH at `path` (None: leave the settings as they are)."""
    from app.config import get_settings

    old = os.environ.get("QADAM_CITY_GAPS_PATH")
    if path is not None:
        os.environ["QADAM_CITY_GAPS_PATH"] = str(path)
        get_settings.cache_clear()
    try:
        yield
    finally:
        if path is not None:
            if old is None:
                os.environ.pop("QADAM_CITY_GAPS_PATH", None)
            else:
                os.environ["QADAM_CITY_GAPS_PATH"] = old
            get_settings.cache_clear()


def inprocess_get(
    map_path: Path | None = None,
) -> tuple[GetFn, Callable[[], list[str]], Callable[[], None]]:
    """GET on the app itself (a test client); logs are captured. The weather warm-up of the
    lifespan is switched off, so nothing goes to Open-Meteo. map_path: serve this map file
    (QADAM_CITY_GAPS_PATH) instead of the configured one."""
    from fastapi.testclient import TestClient

    from app.api import create_app
    from eval.run_eval import capture_logs

    captured: list[str] = []
    with city_gaps_file(map_path), \
            mock.patch("app.api.start_weather_warm_up", lambda deps: None):
        client = TestClient(create_app())
        client.__enter__()  # lifespan: the app loads its data like a server would

    def get(params: dict[str, str], path: str = CITY_GAPS_PATH) -> tuple[int, str, dict[str, str]]:
        with capture_logs() as records:
            resp = client.get(path, params=params)
        captured.extend(msg for _, msg in records)
        return resp.status_code, resp.text, {k.lower(): v for k, v in resp.headers.items()}

    def logs() -> list[str]:
        return list(captured)

    def close() -> None:
        client.__exit__(None, None, None)

    return get, logs, close


def url_get(base_url: str, timeout_s: float = 30.0) -> GetFn:
    import httpx

    base = base_url.rstrip("/")

    def get(params: dict[str, str], path: str = CITY_GAPS_PATH) -> tuple[int, str, dict[str, str]]:
        try:
            resp = httpx.get(base + path, params=params, timeout=timeout_s)
        except httpx.HTTPError as exc:
            return -1, f"сеть: {type(exc).__name__}", {}
        return resp.status_code, resp.text, {k.lower(): v for k, v in resp.headers.items()}

    return get


def file_get(path: Path) -> GetFn:
    """No server at all: the file itself (for the shape and reconciliation checks only)."""

    def get(params: dict[str, str], path_: str = CITY_GAPS_PATH) -> tuple[int, str, dict[str, str]]:
        try:
            return 200, path.read_text(encoding="utf-8"), {}
        except OSError:
            return 503, json.dumps(UNAVAILABLE), {}

    return get


# ---------------------------------------------------------------------- run


def run_checks(get: GetFn, log_lines: Callable[[], list[str]] | None, reconcile: bool,
               privacy: bool = True, processed: Path = PROCESSED, health: bool = True,
               get_without_file: GetFn | None = None) -> tuple[list[Check], list[str]]:
    """health: also /api/health (not for --file-only); get_without_file: the same app with no
    map file (in-process) for the 503 / «missing» / unchanged status checks."""
    notes: list[str] = []
    status, text, _ = get({})
    if status < 0:
        return [Check("endpoint", "GET /api/city-gaps отвечает", False,
                      f"сервер не ответил ({text})")], notes
    if status == 404:
        return [Check("endpoint", "GET /api/city-gaps есть", False,
                      "404: эндпоинта нет (FEATURES T142 не влит)")], notes
    checks: list[Check] = []
    if health:
        checks += check_health(get)
    if get_without_file is not None:
        checks += check_missing_file(get, get_without_file)
    if status != 200:
        return checks + [Check("endpoint", "GET /api/city-gaps отвечает 200", False,
                               f"HTTP {status}: файла карты нет или он не прошёл схему")], notes
    gaps, shape = check_shape(_json(text))
    checks += shape
    if gaps is not None:
        checks += check_districts(gaps)
    if gaps is not None and reconcile:
        try:
            checks += check_reconcile(gaps, local_totals(processed))
        except (OSError, KeyError, ValueError) as exc:
            checks.append(Check("reconcile", "сверка с отчётом DATA", False,
                                f"файлы DATA не читаются: {type(exc).__name__}"))
    elif gaps is not None:
        notes.append("сверка с REPORT.md и walk_coverage.json не выполнена: --url, у сервера "
                     "могут быть другие данные (включить: --local-data-check)")
    if privacy:
        checks += check_privacy(get, log_lines)
    return checks, notes


def run_inprocess(map_path: Path | None = None) -> tuple[list[Check], list[str]]:
    """The app with its map file, and once more with no map file (a path that does not exist)."""
    with ExitStack() as stack:
        get, logs, close = inprocess_get(map_path)
        stack.callback(close)
        tmp = stack.enter_context(tempfile.TemporaryDirectory())
        get_without, _, close_without = inprocess_get(Path(tmp) / "no_city_gaps.json")
        stack.callback(close_without)
        return run_checks(get, logs, reconcile=True, get_without_file=get_without)


def summary(checks: list[Check]) -> str:
    return f"{sum(c.passed for c in checks)}/{len(checks)}"


def render(mode: str, checks: list[Check], notes: list[str]) -> str:
    lines = ["# Карта пробелов: проверка eval", "", f"- Режим: `{mode}`",
             f"- **Итог: {summary(checks)}**"]
    lines += [f"- {n}" for n in notes]
    lines += ["", "| id | проверка | итог | подробности |", "|---|---|---|---|"]
    for c in checks:
        lines.append(f"| {c.id} | {c.title} | {'pass' if c.passed else 'FAIL'} "
                     f"| {c.detail.replace('|', '/')} |")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Qadam city gaps map checks (T144)")
    src = ap.add_mutually_exclusive_group()
    src.add_argument("--url", help="live API base URL, e.g. http://localhost:8010")
    src.add_argument("--file", type=Path,
                     help="check this file: in-process the app serves it (QADAM_CITY_GAPS_PATH)")
    ap.add_argument("--local-data-check", action="store_true",
                    help="with --url: reconcile with the local REPORT.md / walk_coverage.json too")
    ap.add_argument("--file-only", action="store_true",
                    help="no app: read --file directly (shape and reconciliation only)")
    ap.add_argument("--out", type=Path, default=Path(__file__).resolve().parent)
    args = ap.parse_args(argv)

    notes: list[str] = []
    if args.url:
        mode = f"url {args.url}"
        checks, notes = run_checks(url_get(args.url), None, reconcile=args.local_data_check)
        notes.append("лог сервера eval не видит: проверка лога — только в процессе")
    elif args.file_only and args.file:
        mode = f"file {args.file}"
        checks, notes = run_checks(file_get(args.file), None, reconcile=True, privacy=False,
                                   health=False)
    else:
        mode = "in-process" + (f" --file {args.file}" if args.file else "")
        checks, notes = run_inprocess(args.file)
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "report.city_gaps.md").write_text(render(mode, checks, notes), encoding="utf-8")
    (args.out / "report.city_gaps.json").write_text(json.dumps(
        {"mode": mode, "passed": sum(c.passed for c in checks), "total": len(checks),
         "notes": notes, "checks": [asdict(c) for c in checks]},
        ensure_ascii=False, indent=2), encoding="utf-8")
    for c in checks:
        print(f"{'pass' if c.passed else 'FAIL'}  {c.id:<24} {c.detail[:110]}")
    for n in notes:
        print(f"note: {n}")
    print(f"CITY GAPS {mode}: {summary(checks)} -> {args.out / 'report.city_gaps.md'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
