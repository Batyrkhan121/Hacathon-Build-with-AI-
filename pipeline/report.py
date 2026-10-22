"""data/processed/REPORT.md: what the dataset contains and where it is thin.

    python -m pipeline.report

Reads data/processed/poi.jsonl, the raw chunks in data/raw/ (OSM-only facts such as the
wikidata tag) and data/landmarks.json (to label dense areas).
"""

from __future__ import annotations

import argparse
import json
import logging
import statistics
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import datetime
from pathlib import Path

import h3

from app.config import get_settings
from app.hours import ASTANA_TZ, evaluate_hours
from app.schemas import GAP_MIN_CROSSINGS, GAP_MIN_POI, GAP_MIN_WALK_M, POI, CityGaps
from pipeline import city_gaps as gaps_map
from pipeline import districts as district_data
from pipeline.city_gaps import districts_add_up
from pipeline.districts import district_status
from pipeline.fetch_osm import area_chunks, is_partial
from pipeline.landmark_refs import LANDMARK_OSM
from pipeline.normalize import (
    WARM_CATEGORIES,
    fetched_date,
    haversine_m,
    load_outlines,
    load_payloads,
    osm_base,
    raw_files,
)

log = logging.getLogger(__name__)

# a landmark POI = the OSM object of a landmark that our selectors did not fetch, so
# normalize.finalize added it; "manual" in sources is not a marker (overrides add it too)
LANDMARK_POI_IDS = frozenset(f"osm:{ref}" for ref in LANDMARK_OSM.values())
DENSITY_RES = 7  # ~5 km2 hexagons
SPARSE_MAX = 3
LANDMARK_LABEL_M = 2500
# a weekday noon: only checks that app.hours understands the opening_hours string
HOURS_PARSE_AT = datetime(2026, 10, 21, 12, 0, tzinfo=ASTANA_TZ)


def pct(part: int, total: int) -> str:
    return f"{100 * part / total:.0f}%" if total else "—"


def raw_tags_by_id(payloads: Iterable[Mapping]) -> dict[str, Mapping[str, str]]:
    out: dict[str, Mapping[str, str]] = {}
    for payload in payloads:
        for el in payload.get("elements", []):
            out.setdefault(f"osm:{el.get('type')}/{el.get('id')}", el.get("tags") or {})
    return out


def warm_lines(pois: Sequence[POI]) -> list[str]:
    """The «в тепле» label: how many such places there are and how many have usable hours."""
    warm = [p for p in pois if p.category in WARM_CATEGORIES]
    known = [p for p in warm if p.opening_hours]
    parsed = [p for p in known
              if evaluate_hours(p.opening_hours, HOURS_PARSE_AT).status != "unknown"]
    by_cat = Counter(p.category for p in warm)
    lines = [
        "",
        "## Метка «в тепле»",
        "ТРЦ, музеи, библиотеки, кафе, театры и кино по категории OSM (`WARM_CATEGORIES` в "
        f"pipeline/normalize.py), мест: **{len(warm)}**. Часы работы известны у {len(known)} "
        f"({pct(len(known), len(warm))}), из них ядро (app.hours) разбирает {len(parsed)}: "
        "только у них карточка может сказать «открыто сейчас». Тег `indoor` шире "
        f"(мест: {sum(1 for p in pois if p.indoor)}; ещё рестораны, бары, храмы и т. п.).",
        "",
        "| Категория | мест | часы известны | разбираются |",
        "|---|---:|---:|---:|",
        *(f"| {cat} | {by_cat[cat]} | {sum(1 for p in known if p.category == cat)} | "
          f"{sum(1 for p in parsed if p.category == cat)} |"
          for cat in sorted(WARM_CATEGORIES, key=lambda c: (-by_cat[c], c))),
    ]
    return lines


def nearest_landmark(lat: float, lon: float, landmarks: Sequence[Mapping]) -> str:
    best = min(
        landmarks,
        key=lambda lm: haversine_m(lat, lon, lm["geo"]["lat"], lm["geo"]["lon"]),
        default=None,
    )
    if best is None:
        return ""
    dist = haversine_m(lat, lon, best["geo"]["lat"], best["geo"]["lon"])
    return best["names"]["ru"] if dist <= LANDMARK_LABEL_M else ""


def walk_graph_lines(meta: Mapping) -> list[str]:
    """The «Пешеходный граф» section, read from data/processed/walk_graph_meta.json."""
    loaded, total = len(meta.get("tiles_loaded") or []), meta.get("tiles_total") or 0
    lines = [
        "",
        "## Пешеходный граф",
        "Файл `data/processed/walk_graph.npz` (контракт: docs/ARCHITECTURE.md, раздел 5), "
        "собирает `python -m pipeline.walk_graph`. Источник — дороги и тропинки OSM, по "
        "которым можно идти пешком (без motorway/trunk, foot=no и access=private; частная "
        "дорога остаётся, если foot=yes/designated/permissive разрешает пешехода).",
        f"- Снимок OSM: {meta.get('osm_snapshot') or '—'}"
        + (f" (плитки из разных снимков: {', '.join(meta['osm_snapshots'])}; указан самый старый)"
           if len(meta.get("osm_snapshots") or []) > 1 else "")
        + f"; собран {meta.get('created_at', '—')}.",
        f"- Узлов: {meta.get('nodes', 0)}; рёбер: {meta.get('edges', 0)}; длина сети: "
        f"{meta.get('total_length_km', '—')} км; в крупнейшей связной компоненте "
        f"{100 * (meta.get('largest_component_share') or 0):.1f}% узлов (остальные отброшены).",
        f"- Переходов: {meta.get('crossings', 0)} (из них нерегулируемых без разметки "
        f"{meta.get('unmarked_crossings', 0)}"
        + (f", с известной разметкой {meta['crossings_marked']}"
           + (f" — в том числе {meta['crossings_marked_signals']} со светофором, маршрут "
              "покажет их как светофор" if "crossings_marked_signals" in meta else "")
           if "crossings_marked" in meta else "")
        + f"); светофоров: {meta.get('signals', 0)}; "
        f"узлов с понижённым бордюром: {meta.get('kerbs_lowered', 0)}"
        + (f" (из них переходов {meta['crossings_kerb_lowered']}), с непониженным: "
           f"{meta.get('kerbs_raised', 0)} (переходов {meta.get('crossings_kerb_raised', 0)})"
           if "crossings_kerb_lowered" in meta else "")
        + ". Тип перехода взят с линии "
        f"footway=crossing у {meta.get('crossings_from_way_tags', 0)} узлов (из них "
        f"{meta.get('crossing_nodes_added', 0)} новых, в середине перехода без узла в OSM).",
        f"- Рёбер с лестницами: {meta.get('steps_edges', 0)}; освещённых: "
        f"{meta.get('lit_edges', 0)}"
        + (f"; неосвещённых (lit=no): {meta['unlit_edges']}" if "unlit_edges" in meta else "")
        + f"; рёбер-переходов: {meta.get('crossing_edges', 0)}.",
        f"- POI привязано к узлу (≤{meta.get('snap_max_m', 150)} м): "
        f"{meta.get('poi_snapped', 0)} из {meta.get('poi_total', 0)} "
        f"({100 * (meta.get('poi_snapped_share') or 0):.1f}%)"
        + (f", медиана {meta['poi_snap_median_m']:.0f} м"
           if meta.get("poi_snap_median_m") is not None else "")
        + f"; ориентиров: {meta.get('landmarks_snapped', 0)} из {meta.get('landmarks_total', 0)}.",
        f"- Размер файла: {(meta.get('file_bytes') or 0) / 1e6:.1f} МБ; лицензия: "
        f"{meta.get('license') or 'ODbL (производная OSM)'}.",
    ]
    if meta.get("nodes_outside_bbox"):
        lines.append(
            f"- Узлы за границей bbox: {meta['nodes_outside_bbox']} — дороги, пересекающие "
            "границу, берутся целиком (окраинные трассы, подъезд к аэропорту); фактический "
            f"охват узлов {meta.get('nodes_bbox')}."
        )
    if meta.get("coverage") == "full":
        lines.append(f"- Покрытие: весь город, {loaded} из {total} плиток.")
    else:
        lines.append(
            f"- **Покрытие частичное: {loaded} из {total} плиток** "
            f"({', '.join(meta.get('tiles_loaded') or []) or '—'}); не скачаны: "
            f"{', '.join(meta.get('tiles_missing') or []) or '—'}"
            + (f"; неполные ответы Overpass: {', '.join(meta['tiles_partial'])}"
               if meta.get("tiles_partial") else "")
            + ". Маршрут вне покрытых плиток граф не построит. Среди POI внутри покрытых "
            f"плиток ({meta.get('poi_in_coverage', '—')}) привязано "
            f"{100 * (meta.get('poi_snapped_share_in_coverage') or 0):.1f}%."
        )
    return lines


LIT_LABELS = {"yes": "освещено (lit=yes)", "no": "не освещено (lit=no)",
              "unknown": "нет данных об освещении"}
CROSSING_LABELS = {
    "signals": "со светофором",
    "marked": "с разметкой (zebra, marked, uncontrolled, crossing:markings)",
    "unmarked": "без разметки (unmarked, crossing:markings=no)",
    "unknown": "тип не указан",
}
KERB_LABELS = {
    "lowered": "занижен (lowered, flush, kerb=no)",
    "raised": "не занижен хотя бы с одной стороны (raised, rolled)",
    "unknown": "нет данных",
}


def walk_coverage_lines(cov: Mapping) -> list[str]:
    """Tag coverage of the walk graph, from data/processed/walk_coverage.json."""
    def row(label: str, item: Mapping) -> str:
        return (f"| {label} | {item['km']} | {100 * item['length_share']:.1f}% | "
                f"{item['edges']} | {100 * item['edges_share']:.1f}% |")

    lit, foot, steps = cov["lit"], cov["lit_footways"], cov["steps"]
    lines = [
        "",
        "## Покрытие тегов OSM по пешеходному графу",
        f"Считает `python -m pipeline.walk_coverage` по тому же графу (снимок "
        f"{(cov.get('osm_snapshot') or '—')[:10]}): {cov['nodes']} узлов, {cov['edges']} рёбер, "
        f"{cov['length_km']} км, из них тротуары и дорожки {cov['footway_length_km']} км.",
        "",
        "| Рёбра | км | % длины | рёбер | % рёбер |",
        "|---|---:|---:|---:|---:|",
        *(row(LIT_LABELS[k], lit[k]) for k in LIT_LABELS),
        *(row(f"тротуары и дорожки: {LIT_LABELS[k]}", foot[k]) for k in LIT_LABELS),
        row("ступени (highway=steps)", steps),
        "",
        f"| Переходы: {cov['crossings_total']} узлов | узлов | % |",
        "|---|---:|---:|",
        *(f"| {CROSSING_LABELS[k]} | {v['nodes']} | {100 * v['share']:.1f}% |"
          for k, v in cov["crossings"].items()),
        *(f"| бордюр: {KERB_LABELS[k]} | {v['nodes']} | {100 * v['share']:.1f}% |"
          for k, v in cov["kerbs"].items()),
        "",
        "Освещённость и бордюр — биты npz (edge 2/32, node 4/16), как их видит маршрут; нет "
        "бита — «нет данных», а не «да». Бордюр перехода — kerb=* на его узле и на узлах линии "
        "footway=crossing. Тип перехода — тоже по битам (2 светофор, 8 без разметки, 32 "
        "разметка известна); без них тип неизвестен, и маршрут не называет такой переход "
        f"«зеброй». Ступеней с тегами пандуса (ramp*): {steps['with_ramp_tags']} из "
        f"{steps['edges']}.",
    ]
    if cov.get("same_graph_as_npz") is False:
        lines.append("**Граф не совпал с walk_graph.npz по числу узлов и рёбер: пересоберите "
                     "оба из одних и тех же сырых файлов.**")
    return lines


def _share(part: float, whole: float, enough: bool) -> str:
    return f"{100 * part / whole:.1f}%" if enough and whole else "мало данных"


def _district_row(name: str, counts: Mapping[str, int] | None, cells: int, note: str) -> str:
    """One district (GapCounts): totals and «нет данных» shares only, no «есть» split: the
    table shows gaps in open data, it does not rate districts."""
    if counts is None:
        return f"| {name} | {note} |" + " — |" * 10
    walk, crossings, places = counts["walk_m"], counts["crossings"], counts["poi"]
    lit_ok, cross_ok, poi_ok = (walk >= GAP_MIN_WALK_M, crossings >= GAP_MIN_CROSSINGS,
                                places >= GAP_MIN_POI)
    return (
        f"| {name} | {note or 'ok'} | {cells} | {walk / 1000:.1f} | "
        f"{_share(counts['walk_lit_unknown_m'], walk, lit_ok)} | "
        f"{crossings} | {_share(counts['crossings_kind_unknown'], crossings, cross_ok)} | "
        f"{_share(counts['crossings_kerb_unknown'], crossings, cross_ok)} | {places} | "
        f"{_share(counts['poi_no_hours'], places, poi_ok)} | "
        f"{_share(counts['poi_wheelchair_unknown'], places, poi_ok)} | "
        f"{_share(counts['poi_no_name_kk'], places, poi_ok)} |"
    )


def city_gaps_lines(gaps: Mapping, checks: Sequence[Mapping] | None = (),
                    districts_file: Mapping | None = None) -> list[str]:
    """«Карта пробелов»: city totals of data/processed/city_gaps.json, the check against
    walk_coverage.json and poi.jsonl (None: it could not run), and its districts block with
    the reasons and the OSM districts off the list from data/processed/districts.json."""
    city, cells = gaps["city"], gaps.get("cells") or []

    def row(layer: str, whole: float, part: float, unit: str = "") -> str:
        share = f"{100 * part / whole:.1f}%" if whole else "—"
        return f"| {layer} | {whole}{unit} | {part}{unit} | {share} |"

    lines = [
        "",
        "## Карта пробелов",
        "`data/processed/city_gaps.json` (контракт 1.4.1, `CityGaps`) собирает "
        "`python -m pipeline.city_gaps` из `poi.jsonl` и пешеходного графа: где в открытых "
        f"данных OSM нет сведений, по {len(cells)} ячейкам H3 r{gaps.get('h3_res', 8)} и по "
        f"городу. Места на {gaps.get('poi_date')}, граф на {gaps.get('walk_graph_date')}; "
        f"{gaps.get('attribution')}, {gaps.get('license')}. Доля — «в OSM нет сведений», "
        "а не оценка района.",
        "",
        "| Слой | всего | нет данных | доля |",
        "|---|---:|---:|---:|",
        row("часы работы", city["poi"], city["poi_no_hours"]),
        row("доступность для коляски", city["poi"], city["poi_wheelchair_unknown"]),
        row("название на казахском", city["poi"], city["poi_no_name_kk"]),
        row("освещённость, км путей", round(city["walk_m"] / 1000, 1),
            round(city["walk_lit_unknown_m"] / 1000, 1)),
        row("тип перехода", city["crossings"], city["crossings_kind_unknown"]),
        row("бордюр на переходе", city["crossings"], city["crossings_kerb_unknown"]),
        "",
        f"Ячеек «мало данных» (пороги `GAP_MIN_*`): мест меньше {GAP_MIN_POI} — "
        f"{sum(1 for c in cells if c['poi'] < GAP_MIN_POI)}, путей меньше {GAP_MIN_WALK_M} м — "
        f"{sum(1 for c in cells if c['walk_m'] < GAP_MIN_WALK_M)}, переходов меньше "
        f"{GAP_MIN_CROSSINGS} — {sum(1 for c in cells if c['crossings'] < GAP_MIN_CROSSINGS)}.",
    ]
    if checks is None:
        lines += ["", "Сверка не выполнена: `walk_coverage.json` неполный или не читается "
                  "(см. лог); пересоберите его: `python -m pipeline.walk_coverage`."]
    elif checks:
        lines += [
            "",
            "Сверка итогов карты с `walk_coverage.json` (таблицы выше) и `poi.jsonl`; допуски "
            "ARCHITECTURE: числа точно, длина до 0,5 км, доли до 0,1 п. п.",
            "",
            "| Что | карта | отчёт | сходится |",
            "|---|---:|---:|---|",
            *(f"| {c['what']} | {c['map']} | {c['report']} | {'да' if c['ok'] else '**нет**'} |"
              for c in checks),
        ]
    block = gaps.get("districts")
    if not block:
        lines += ["", "Районов нет: `data/processed/districts.json` нет "
                  "(`python -m pipeline.city_gaps --fetch-districts`) — только ячейки."]
        return lines
    file_by_id = {e["id"]: e for e in (districts_file or {}).get("districts", [])}
    changes = (districts_file or {}).get("composition_changes") or []
    checked = (f"сверен с официальным списком акимата {block['official_list_url']} "
               f"({block['official_list_checked']})" if block.get("official_list_url")
               else "**не сверен** с официальным списком")

    def note(d: Mapping) -> str:
        if d["status"] == "ok":
            return "ok"
        if d["status"] == "missing":
            return "границы нет в OSM"
        why = district_status(file_by_id[d["id"]], changes)[1] if d["id"] in file_by_id else ""
        return f"граница в OSM устарела ({d['boundary_date']})" + (f": {why}" if why else "")

    adds_up = districts_add_up(gaps)
    lines += [
        "",
        f"По районам ({len(block['items'])}; список районов {checked}; границы OSM на "
        f"{block['boundaries_date']}). Ячейка — к району, в контуре которого её центр. Это "
        "пробелы в открытых данных, а не оценка района; доля — при "
        f"{GAP_MIN_POI}+ местах, {GAP_MIN_WALK_M}+ м путей, {GAP_MIN_CROSSINGS}+ переходах:",
        "",
        "| Район | граница | ячеек | км путей | освещённость: нет данных | переходов | "
        "тип перехода: нет данных | бордюр: нет данных | мест | часы: нет данных | "
        "коляска: нет данных | название kk: нет |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
        *(_district_row(d["name"]["ru"], d.get("counts"), d["cell_count"], note(d))
          for d in sorted(block["items"], key=lambda d: d["name"]["ru"])),
        _district_row("вне границ районов", block["unassigned"], block["unassigned_cells"], "—"),
        "",
        "Сверка: районы + вне границ районов = город по всем девяти счётчикам — "
        + ("да." if adds_up else "**нет**."),
    ]
    aside = (districts_file or {}).get("osm_not_in_list") or []
    lines += [f"- В OSM есть район вне официального списка: {a['name'].get('name')} "
              f"(relation {a['osm_relation']}, {a['why']}) — в карту не вошёл." for a in aside]
    return lines


SOURCE_KIND_RU = {"venue": "площадки", "organizer": "организаторы",
                  "city_portal": "городские порталы"}


def events_lines(summary: Mapping) -> list[str]:
    """Hand-added events (pipeline.events): how many passed the check, by source."""
    bound = summary.get("bound_by") or {}
    by_source = summary.get("by_source") or {}
    lines = [
        "",
        "## События",
        "Вносят люди в `data/manual/events.json` (только факты и ссылка на источник из "
        "`data/manual/event_sources.json`), проверяет и привязывает к POI "
        "`python -m pipeline.events`, результат — `data/events.json`.",
        f"- Записей: {summary.get('total', 0)}; прошли проверку: {summary.get('valid', 0)}; "
        f"предстоящих в data/events.json: {summary.get('upcoming', 0)}; прошедших: "
        f"{summary.get('past', 0)}; ошибок: {len(summary.get('errors') or [])}.",
        "- Площадка привязана к POI: по id " f"{bound.get('id', 0)}, по названию "
        f"{bound.get('name', 0)}, ближайшее место {bound.get('nearest', 0)}; не привязана "
        f"{bound.get('none', 0)}.",
        "- По источникам: "
        + (", ".join(f"{name} — {n}" for name, n in by_source.items()) or "событий пока нет")
        + ("; по видам: " + ", ".join(f"{SOURCE_KIND_RU.get(k, k)} — {n}"
                                       for k, n in summary["by_kind"].items())
           if summary.get("by_kind") else "")
        + ".",
    ]
    lines += [f"- Ошибка: {e}" for e in (summary.get("errors") or [])[:10]]
    return lines


def wheelchair_lines(pois: Sequence[POI]) -> list[str]:
    counts = Counter(p.wheelchair for p in pois)
    n = len(pois)
    return [
        "",
        "## Доступность (wheelchair у POI)",
        "| wheelchair | мест | % |",
        "|---|---:|---:|",
        *(f"| {value} | {counts[value]} | {100 * counts[value] / n if n else 0:.1f}% |"
          for value in ("yes", "limited", "no", "unknown")),
    ]


def _safe(title: str, render: Callable[..., list[str]], *args: object) -> list[str]:
    """A section whose processed file is broken or incomplete becomes a note in the report,
    not a failed report (and not a failed run_all)."""
    try:
        return render(*args)
    except (KeyError, TypeError, ValueError, ZeroDivisionError) as exc:
        log.exception("report section %r skipped", title)
        return ["", f"## {title}",
                f"- Раздел не собран: файл неполный или повреждён ({type(exc).__name__})."]


def build_report(
    pois: Sequence[POI],
    payloads: Sequence[Mapping],
    landmarks: Sequence[Mapping],
    *,
    missing_chunks: Sequence[str] | None = None,
    notes: Sequence[str] = (),
    generated_at: datetime | None = None,
    outlines_meta: Mapping | None = None,
    walk_meta: Mapping | None = None,
    walk_coverage: Mapping | None = None,
    events: Mapping | None = None,
    city_gaps: Mapping | None = None,
    city_gaps_checks: Sequence[Mapping] | None = (),
    city_gaps_districts: Mapping | None = None,
    city_gaps_error: str | None = None,
) -> str:
    """`missing_chunks`: city tiles not on disk; None when the build is not the whole city.
    `outlines_meta`: the `qadam` block of data/raw/osm_mall_outlines.json, if any.
    `walk_meta`, `walk_coverage`: data/processed/walk_graph_meta.json, walk_coverage.json.
    `events`: the summary of pipeline.events (data/manual/events.json), if checked.
    `city_gaps`: data/processed/city_gaps.json; `city_gaps_checks`: pipeline.city_gaps.reconcile;
    `city_gaps_districts`: data/processed/districts.json (reasons, OSM districts off the list);
    `city_gaps_error`: why city_gaps.json is not used (unreadable, fails the schema)."""
    n = len(pois)
    tags_by_id = raw_tags_by_id(payloads)
    now = generated_at or datetime.now(ASTANA_TZ)
    when = now.strftime("%Y-%m-%d %H:%M (UTC+5)")

    def age(stamp: float) -> str:
        at = datetime.fromtimestamp(stamp, ASTANA_TZ)
        hours = int((now - at).total_seconds() // 3600)
        return f"{at.strftime('%Y-%m-%d %H:%M')} ({hours} ч назад)"

    metas = [p.get("qadam", {}) for p in payloads]
    endpoints = Counter(m.get("endpoint", "?") for m in metas)
    stamps = [m["fetched_at_unix"] for m in metas if m.get("fetched_at_unix")]
    raw_elements = sum(len(p.get("elements", [])) for p in payloads)

    lines = [
        "# Данные Qadam: отчёт сборки",
        "",
        f"Сформирован: {when}. Источник: OpenStreetMap (© участники OSM, лицензия ODbL), "
        "выгрузка через Overpass API. Файл генерирует `python -m pipeline.report`.",
        "",
        "## Выгрузка",
        f"- Кусков выгрузки: {len(payloads)} "
        f"({', '.join(m.get('chunk') or m.get('area') or '?' for m in metas)}).",
        f"- Сырых элементов: {raw_elements}; после нормализации и дедупликации: {n} POI.",
        f"- Добавлено вручную: {sum(1 for p in pois if p.id.startswith('manual:'))} "
        "(места из эталона роли A, которых нет в OSM; источник — официальный сайт в `website`).",
        "- Ориентиры, добавленные как места (объект OSM + ручная разметка): "
        f"{sum(1 for p in pois if p.id in LANDMARK_POI_IDS and p.id not in tags_by_id)}.",
    ]
    if stamps:
        lo = datetime.fromtimestamp(min(stamps), ASTANA_TZ).strftime("%Y-%m-%d %H:%M")
        hi = datetime.fromtimestamp(max(stamps), ASTANA_TZ).strftime("%Y-%m-%d %H:%M")
        lines.append(f"- Время выгрузки: {lo} … {hi} (UTC+5).")
        oldest = min((m for m in metas if m.get("fetched_at_unix")),
                     key=lambda m: m["fetched_at_unix"])
        lines.append(
            f"- Возраст данных: самый старый кусок {oldest.get('chunk') or oldest.get('area')} "
            f"скачан {age(oldest['fetched_at_unix'])}; при сбое выгрузки берутся файлы с диска."
        )
    outline_stamp = (outlines_meta or {}).get("fetched_at_unix")
    lines.append(
        f"- Контуры ТРЦ: скачаны {age(outline_stamp)}."
        if outline_stamp
        else "- Контуры ТРЦ: не скачаны — проверка «внутри ТРЦ» (indoor) не работала."
    )
    lines.append(
        "- Зеркала Overpass: " + ", ".join(f"{u} ({c})" for u, c in endpoints.most_common()) + "."
    )
    snapshots: dict[str, list[str]] = defaultdict(list)
    for payload in payloads:
        if osm_base(payload):
            host = payload.get("qadam", {}).get("endpoint", "?").split("/")[2:3] or ["?"]
            snapshots[host[0]].append(fetched_date(payload))
    if snapshots:
        parts = [
            f"{host}: {min(d)}" + (f" … {max(d)}" if min(d) != max(d) else "")
            for host, d in sorted(snapshots.items())
        ]
        lines.append(
            "- Дата снимка OSM по зеркалам: " + "; ".join(parts) + ". Пересекающиеся куски "
            "берутся из самого свежего снимка; `fetched_at` у записи — дата её снимка."
        )
    partial = [m.get("chunk") or m.get("area") or "?" for p, m in zip(payloads, metas, strict=True)
               if is_partial(p)]
    if partial:
        lines.append(
            f"- **Неполные ответы Overpass (remark): {', '.join(partial)}.** Данные из них "
            "взяты, но удалёнными по ним ничего не считается; перезапуск выгрузки докачает."
        )
    if missing_chunks is None:
        lines.append(
            "- Покрытие: только перечисленные куски (демо-зоны), без плиток всего города."
        )
    elif missing_chunks:
        lines.append(
            f"- **Не докачано ({len(missing_chunks)}):** {', '.join(missing_chunks)}. "
            "Overpass был нестабилен; повторный запуск `pipeline.fetch_osm --area astana` "
            "докачает только недостающие плитки."
        )
    else:
        lines.append("- Все плитки города скачаны.")

    by_cat = Counter(p.category for p in pois)
    hours_by_cat = Counter(p.category for p in pois if p.opening_hours)
    lines += [
        "",
        "## Категории",
        "| Категория | POI | с часами работы |",
        "|---|---:|---:|",
        *(
            f"| {cat} | {cnt} | {pct(hours_by_cat[cat], cnt)} |"
            for cat, cnt in by_cat.most_common()
        ),
    ]

    lines += warm_lines(pois)
    lines += wheelchair_lines(pois)

    desc_sources = Counter(p.description_source or "нет" for p in pois)
    with_wikidata = sum(1 for p in pois if "wikidata" in tags_by_id.get(p.id, {}))
    lines += [
        "",
        "## Полнота полей",
        f"- `opening_hours`: {pct(sum(1 for p in pois if p.opening_hours), n)}.",
        f"- `wheelchair` указан: {pct(sum(1 for p in pois if p.wheelchair != 'unknown'), n)}.",
        f"- адрес: {pct(sum(1 for p in pois if p.address), n)}; "
        f"сайт или телефон: {pct(sum(1 for p in pois if p.website or p.phone), n)}.",
        f"- тег `wikidata` в OSM: {pct(with_wikidata, n)} ({with_wikidata}).",
        "- описание: "
        + ", ".join(f"{src} — {pct(c, n)} ({c})" for src, c in desc_sources.most_common())
        + ".",
        f"- название на 2+ языках: {pct(sum(1 for p in pois if len(p.name_i18n) > 1), n)}.",
        f"- средний `data_confidence`: {statistics.fmean(p.data_confidence for p in pois):.2f}"
        if n
        else "- средний `data_confidence`: —",
        "- `rating` пуст у всех записей: настоящих рейтингов в открытых данных нет. "
        "`popularity` — документированный прокси (wiki-ссылки, полнота тегов, тип, бренд).",
    ]

    cells: dict[str, list[POI]] = defaultdict(list)
    for p in pois:
        cells[h3.latlng_to_cell(p.geo.lat, p.geo.lon, DENSITY_RES)].append(p)
    sizes = sorted((len(v) for v in cells.values()), reverse=True)
    sparse = sum(1 for s in sizes if s <= SPARSE_MAX)
    lines += [
        "",
        f"## Плотность (H3 r{DENSITY_RES}, ≈5 км² на ячейку)",
        f"- Ячеек с данными: {len(cells)}; медиана POI на ячейку: "
        f"{statistics.median(sizes) if sizes else 0}; ячеек с ≤{SPARSE_MAX} POI: {sparse}.",
        "",
        "| Ячейка (центр) | Рядом | POI | с часами | кафе/еда |",
        "|---|---|---:|---:|---:|",
    ]
    food = {"cafe", "restaurant", "fast_food", "bar"}
    ranked = sorted(cells.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    for cell, items in ranked[:8]:
        lat, lon = h3.cell_to_latlng(cell)
        label = nearest_landmark(lat, lon, landmarks) or "—"
        lines.append(
            f"| {lat:.3f}, {lon:.3f} | {label} | {len(items)} | "
            f"{pct(sum(1 for p in items if p.opening_hours), len(items))} | "
            f"{sum(1 for p in items if p.category in food)} |"
        )
    if ranked:
        top_cell, top_items = ranked[0]
        top_lat, top_lon = h3.cell_to_latlng(top_cell)
        top_label = nearest_landmark(top_lat, top_lon, landmarks)
        where = (
            f"рядом с «{top_label}»" if top_label else f"в ячейке {top_lat:.3f}, {top_lon:.3f}"
        )
        with_hours = pct(sum(1 for p in pois if p.opening_hours), n)
        lines += [
            "",
            f"Вывод для слайда «ограничения»: больше всего данных {where} "
            f"({len(top_items)} POI на ≈5 км²); в {sparse} из {len(cells)} ячеек не больше "
            f"{SPARSE_MAX} POI. Часы работы известны у {with_hours} мест — у остальных "
            "карточка пишет «нет данных о часах, проверьте перед визитом».",
        ]

    if notes:
        lines += ["", "## Обогащение и индекс", *(f"- {note}" for note in notes)]

    if walk_meta:
        lines += walk_graph_lines(walk_meta)
    if walk_coverage:
        lines += _safe("Покрытие тегов OSM по пешеходному графу", walk_coverage_lines,
                       walk_coverage)
    if city_gaps_error:
        lines += ["", "## Карта пробелов", f"- {city_gaps_error}: раздел пропущен, пересоберите "
                  "файл: `python -m pipeline.city_gaps`."]
    elif city_gaps:
        lines += _safe("Карта пробелов", city_gaps_lines, city_gaps, city_gaps_checks,
                       city_gaps_districts)
    if events:
        lines += events_lines(events)

    examples = sorted(pois, key=lambda p: (-p.data_confidence, -p.popularity, p.id))[:5]
    lines += ["", "## Примеры записей", "```json"]
    lines += [
        json.dumps(p.model_dump(mode="json", exclude_none=True), ensure_ascii=False)
        for p in examples
    ]
    lines += ["```", ""]
    return "\n".join(lines)


def missing_astana_chunks(raw_dir: Path) -> list[str]:
    return [c.name for c in area_chunks("astana") if not (raw_dir / f"osm_{c.name}.json").exists()]


def events_summary() -> dict:
    """pipeline.events summary; a failing check is an error line, not a failed report."""
    from pipeline import events

    try:
        return events.check_files().summary()
    except Exception as exc:  # noqa: BLE001 - the report must still be written
        return {"errors": [f"проверка событий не выполнилась: {exc}"]}


def write_report(area: str, notes: Sequence[str] = (), out: Path | None = None) -> Path:
    from pipeline.build_index import index_status

    settings = get_settings()
    raw_dir = settings.data_dir / "raw"
    with settings.poi_path.open(encoding="utf-8") as fh:
        pois = [POI.model_validate_json(line) for line in fh if line.strip()]
    payloads = load_payloads(raw_files(area, raw_dir))
    landmarks = json.loads(settings.landmarks_path.read_text(encoding="utf-8"))
    missing = missing_astana_chunks(raw_dir) if area == "astana" else None
    all_notes = [*notes, index_status(settings.index_dir, pois)]
    out = out or settings.poi_path.parent / "REPORT.md"
    outlines = load_outlines(raw_dir) if area != "fixture" else None
    unreadable: dict[str, str] = {}

    def processed(name: str) -> dict | None:
        path = settings.poi_path.parent / name
        if area != "astana" or not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:  # a broken file: a note, not a failed report
            log.error("%s is not readable: %s", name, exc)
            unreadable[name] = type(exc).__name__
            return None

    gaps, coverage = processed("city_gaps.json"), processed("walk_coverage.json")
    all_notes += [f"Файл data/processed/{name} не читается ({why}): его раздел пропущен."
                  for name, why in unreadable.items() if name != "city_gaps.json"]
    checks: list[dict] | None = []
    districts = None
    gaps_error = ("city_gaps.json не читается как JSON" if "city_gaps.json" in unreadable
                  else None)
    if gaps is not None:
        try:
            CityGaps.model_validate(gaps)
        except ValueError:
            log.exception("city_gaps.json fails the CityGaps schema")
            gaps, gaps_error = None, "city_gaps.json не проходит схему CityGaps"
    if gaps is not None:
        try:
            checks = gaps_map.reconcile(gaps["city"], coverage, pois)
        except (KeyError, TypeError, ZeroDivisionError):
            log.exception("city gaps check skipped: walk_coverage.json is incomplete")
            checks = None
        try:
            districts = district_data.load()
        except (OSError, ValueError):  # the reasons are extra, the report is not
            log.exception("districts.json not readable")
    text = build_report(
        pois, payloads, landmarks, missing_chunks=missing, notes=all_notes,
        outlines_meta=(outlines or {}).get("qadam"), walk_meta=processed("walk_graph_meta.json"),
        walk_coverage=coverage,
        events=events_summary() if area == "astana" else None,
        city_gaps=gaps, city_gaps_checks=checks, city_gaps_districts=districts,
        city_gaps_error=gaps_error,
    )
    out.write_text(text, encoding="utf-8")
    return out


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--area", choices=["astana", "zones", "fixture"], default="astana")
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)
    print(f"report -> {write_report(args.area, out=args.out)}")


if __name__ == "__main__":
    main()
