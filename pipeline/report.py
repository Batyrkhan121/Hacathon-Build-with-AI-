"""data/processed/REPORT.md: what the dataset contains and where it is thin.

    python -m pipeline.report

Reads data/processed/poi.jsonl, the raw chunks in data/raw/ (OSM-only facts such as the
wikidata tag) and data/landmarks.json (to label dense areas).
"""

from __future__ import annotations

import argparse
import json
import statistics
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from pathlib import Path

import h3

from app.config import get_settings
from app.hours import ASTANA_TZ
from app.schemas import POI
from pipeline.fetch_osm import area_chunks
from pipeline.normalize import haversine_m, load_payloads, raw_files

DENSITY_RES = 7  # ~5 km2 hexagons
SPARSE_MAX = 3
LANDMARK_LABEL_M = 2500


def pct(part: int, total: int) -> str:
    return f"{100 * part / total:.0f}%" if total else "—"


def raw_tags_by_id(payloads: Iterable[Mapping]) -> dict[str, Mapping[str, str]]:
    out: dict[str, Mapping[str, str]] = {}
    for payload in payloads:
        for el in payload.get("elements", []):
            out.setdefault(f"osm:{el.get('type')}/{el.get('id')}", el.get("tags") or {})
    return out


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


def build_report(
    pois: Sequence[POI],
    payloads: Sequence[Mapping],
    landmarks: Sequence[Mapping],
    *,
    missing_chunks: Sequence[str] = (),
    notes: Sequence[str] = (),
    generated_at: datetime | None = None,
) -> str:
    n = len(pois)
    tags_by_id = raw_tags_by_id(payloads)
    when = (generated_at or datetime.now(ASTANA_TZ)).strftime("%Y-%m-%d %H:%M (UTC+5)")
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
        f"- Кусков выгрузки: {len(payloads)} ({', '.join(m.get('chunk', '?') for m in metas)}).",
        f"- Сырых элементов: {raw_elements}; после нормализации и дедупликации: {n} POI.",
    ]
    if stamps:
        lo = datetime.fromtimestamp(min(stamps), ASTANA_TZ).strftime("%Y-%m-%d %H:%M")
        hi = datetime.fromtimestamp(max(stamps), ASTANA_TZ).strftime("%Y-%m-%d %H:%M")
        lines.append(f"- Время выгрузки: {lo} … {hi} (UTC+5).")
    lines.append(
        "- Зеркала Overpass: " + ", ".join(f"{u} ({c})" for u, c in endpoints.most_common()) + "."
    )
    if missing_chunks:
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
    for cell, items in sorted(cells.items(), key=lambda kv: -len(kv[1]))[:8]:
        lat, lon = h3.cell_to_latlng(cell)
        label = nearest_landmark(lat, lon, landmarks) or "—"
        lines.append(
            f"| {lat:.3f}, {lon:.3f} | {label} | {len(items)} | "
            f"{pct(sum(1 for p in items if p.opening_hours), len(items))} | "
            f"{sum(1 for p in items if p.category in food)} |"
        )
    lines += [
        "",
        "Вывод для слайда «ограничения»: данные плотные в центре левого берега и у EXPO, "
        "на окраинах POI мало и почти нет часов работы — там рекомендации беднее, "
        "а карточки чаще пишут «нет данных о часах, проверьте перед визитом».",
    ]

    if notes:
        lines += ["", "## Обогащение и индекс", *(f"- {note}" for note in notes)]

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


def write_report(area: str, notes: Sequence[str] = (), out: Path | None = None) -> Path:
    from pipeline.build_index import index_status

    settings = get_settings()
    raw_dir = settings.data_dir / "raw"
    with settings.poi_path.open(encoding="utf-8") as fh:
        pois = [POI.model_validate_json(line) for line in fh if line.strip()]
    payloads = load_payloads(raw_files(area, raw_dir))
    landmarks = json.loads(settings.landmarks_path.read_text(encoding="utf-8"))
    missing = missing_astana_chunks(raw_dir) if area == "astana" else []
    all_notes = [*notes, index_status(settings.index_dir, len(pois))]
    out = out or settings.poi_path.parent / "REPORT.md"
    text = build_report(pois, payloads, landmarks, missing_chunks=missing, notes=all_notes)
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
