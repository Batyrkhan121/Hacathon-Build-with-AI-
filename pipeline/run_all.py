"""The whole data pipeline in one idempotent command.

    python -m pipeline.run_all                        # Astana: fetch -> normalize -> ... -> report
    python -m pipeline.run_all --area zones           # demo zones only (fast)
    python -m pipeline.run_all --skip-fetch           # rebuild from data/raw/ without network
    python -m pipeline.run_all --deadline 16:30       # retry missing tiles until then

Steps: fetch (Overpass, cached chunks) -> normalize (data/processed/poi.jsonl) -> enrich
(Wikidata + Claude descriptions, when pipeline.enrich exists and keys are set) -> build_index
(Gemini embeddings, only with GEMINI_API_KEY) -> report (data/processed/REPORT.md).
A failed network step never stops the run: the next steps work with what is on disk.
"""

from __future__ import annotations

import argparse
import importlib
import logging
import time
from collections.abc import Sequence

from app.config import get_settings
from app.schemas import POI
from pipeline import (
    build_index,
    city_gaps,
    etalon,
    events,
    fetch_osm,
    normalize,
    overrides,
    report,
)

log = logging.getLogger("pipeline.run_all")


def load_pois() -> list[POI]:
    with get_settings().poi_path.open(encoding="utf-8") as fh:
        return [POI.model_validate_json(line) for line in fh if line.strip()]


def run(
    area: str = "astana",
    *,
    skip_fetch: bool = False,
    skip_enrich: bool = False,
    limit: int | None = None,
    deadline: str | None = None,
    force: bool = False,
    skip_index: bool = False,
) -> list[str]:
    """Returns the notes that went into REPORT.md."""
    settings = get_settings()
    notes: list[str] = []
    started = time.time()

    if not skip_fetch:
        if deadline:
            res = fetch_osm.fetch_until(area, fetch_osm.deadline_today(deadline), force=force)
        else:
            res = fetch_osm.fetch_area(area, force=force)
        log.info("fetch: %d chunks ready (%d cached), %d failed",
                 len(res.done), len(res.cached), len(res.failed))
        if res.failed:
            notes.append(f"Выгрузка: не скачано {len(res.failed)} кусков, взяты данные с диска.")
        if area != "fixture" and fetch_osm.fetch_mall_outlines(force=force) is None:
            notes.append("Контуры ТРЦ не скачаны: проверка «внутри ТРЦ» (indoor) пропущена.")

    paths = normalize.raw_files(area, settings.data_dir / "raw")
    if not paths:
        raise SystemExit(f"no raw OSM data for area {area!r}; Overpass unavailable?")
    stats: dict = {}
    pois = normalize.finalize(
        normalize.normalize_payloads(normalize.load_payloads(paths), stats),
        normalize.load_landmarks(settings.landmarks_path),
    )
    wheelchair = stats.get("wheelchair_other") or {}
    notes.append(
        "Нормализация не взяла: без имени (кроме площадок) "
        f"{stats.get('unnamed', 0)}, без координат {stats.get('no_point', 0)}, вне наших "
        f"категорий {stats.get('no_category', 0)}; склеено дублей {stats.get('duplicates', 0)}; "
        "нестандартный wheelchair → unknown: "
        + (", ".join(f"{v} ×{n}" for v, n in sorted(wheelchair.items())) or "нет")
        + "."
    )
    if stats.get("stale"):
        former = stats.get("stale_pois") or []
        notes.append(
            f"Устаревшие объекты не взяты: {stats['stale']} (есть только в старом снимке "
            "зеркала, а в более свежем полном куске на той же территории их уже нет: закрыты, "
            f"удалены или переразмечены в OSM). Из них подходили под наши категории "
            f"(до склейки дублей) {len(former)}: "
            + "; ".join(former) + "."
        )
    # places inside a mall footprint (rides in Khan Shatyr...) are indoor, by OSM geometry
    pois, inside_malls = normalize.mark_inside_malls(
        pois, normalize.load_outlines(settings.data_dir / "raw")
    )
    # hand-checked reference places: compared with OSM first, the missing ones added
    pois, etalon_notes = etalon.run_on(pois)
    notes.extend(etalon_notes)
    # hand corrections (data/manual/overrides.json), e.g. the EXPO sphere is a museum
    pois, override_notes = overrides.apply_overrides(pois, overrides.load_overrides())
    notes.extend(override_notes)
    unknown = sum(1 for p in pois if p.indoor is None)
    notes.append(
        "indoor только по данным: категория, явный тег OSM indoor=* (так размечены аттракционы "
        "внутри Хан Шатыра) и точка внутри контура shop=mall (добавлено этой проверкой: "
        f"{inside_malls}; остальные места внутри ТРЦ уже имели indoor). Неизвестен у {unknown} "
        "мест — не угадываем; площадки внутри контуров ТРЦ остаются outdoor по категории."
    )
    normalize.write_jsonl(pois, settings.poi_path)
    log.info("normalize: %d POI -> %s", len(pois), settings.poi_path)
    # hand-added events: checked and bound to the fresh POI -> data/events.json; a broken
    # data/manual/events.json is logged and shown in REPORT.md, the rest of the build goes on
    try:
        checked = events.run()
    except Exception:  # noqa: BLE001 - events must never stop the POI build
        log.exception("events: the check failed, data/events.json not updated")
    else:
        for error in checked.errors:
            log.error("events: %s", error)
        log.info("events: %d upcoming, %d errors", len(checked.events), len(checked.errors))

    if skip_enrich:
        notes.append("Обогащение описаниями пропущено (--skip-enrich).")
    else:
        try:
            enrich = importlib.import_module("pipeline.enrich")
        except ModuleNotFoundError as exc:
            if exc.name != "pipeline.enrich":
                raise  # a dependency inside enrich is missing: that is a real error
            notes.append("Обогащение описаниями ещё не подключено (pipeline/enrich.py).")
        else:
            # the area decides which raw chunks give OSM wikidata tags
            notes.extend(enrich.run(limit=limit, area=area))
            pois = load_pois()

    if skip_index:
        # no embedding calls (e.g. the Gemini quota is saved for live queries); an index
        # that no longer matches poi.jsonl is flagged instead of being trusted silently
        status = build_index.index_status(settings.index_dir, pois)
        if "устарел" in status and "не пересобран (" not in status:  # keep a known reason
            build_index.mark_stale(
                settings.index_dir, "пересборка отложена (--skip-index), данные изменились"
            )
        notes.append("Индекс не пересобирался (--skip-index).")
    else:
        idx = build_index.build_index(pois, settings.index_dir)
        log.info("index: %s (%s)", idx.status, idx.reason)
        if idx.status == "skipped":
            notes.append(f"Индекс пропущен: {idx.reason}.")

    if area == "astana":
        # «Карта пробелов» from the final poi.jsonl and the walk graph on disk (built by
        # pipeline.walk_graph); a failure (no graph yet, ...) is logged, the build goes on
        gaps_path = settings.city_gaps_path or settings.poi_path.parent / "city_gaps.json"
        had_map = gaps_path.exists()
        try:
            gaps = city_gaps.run()
        except Exception:  # noqa: BLE001 - the map is extra, the POI build is not
            log.exception("city gaps: not built")
            notes.append("Карта пробелов не собрана (см. лог): "
                         + ("city_gaps.json прежний." if had_map else "city_gaps.json нет."))
        else:
            log.info("city gaps: %d cells", len(gaps.cells))

    out = report.write_report(area, notes=notes)
    log.info("report -> %s; total %.0f s", out, time.time() - started)
    return notes


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--area", choices=["astana", "zones", "fixture"], default="astana")
    parser.add_argument("--skip-fetch", action="store_true", help="use data/raw/ only")
    parser.add_argument("--skip-enrich", action="store_true", help="no Wikidata / Claude")
    parser.add_argument("--limit", type=int, default=None, help="top-N POI for LLM descriptions")
    parser.add_argument("--deadline", default=None, help="HH:MM: stop retrying tiles after it")
    parser.add_argument("--force", action="store_true", help="ignore the 24 h raw cache")
    parser.add_argument("--skip-index", action="store_true", help="no embedding requests")
    args = parser.parse_args(argv)
    logging.basicConfig(level=get_settings().log_level, format="%(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    notes = run(
        args.area,
        skip_fetch=args.skip_fetch,
        skip_enrich=args.skip_enrich,
        limit=args.limit,
        deadline=args.deadline,
        force=args.force,
        skip_index=args.skip_index,
    )
    for note in notes:
        print("NOTE", note)


if __name__ == "__main__":
    main()
