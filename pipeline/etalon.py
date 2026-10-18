"""Hand-made reference list (data/manual/etalon.csv): check our POIs, add what OSM lacks.

    python -m pipeline.etalon        # rewrites poi.jsonl with manual POIs + ETALON.md

The CSV comes from the team (role A): real places with hours and address taken by hand
from the place's official source. Columns are matched by header, case-insensitively
(any order, `,` or `;` delimiter): name / название (required); opening_hours / часы /
часы работы / режим работы; lat / широта; lon / долгота; address / адрес;
source / источник / url / ссылка / сайт; category / категория / тип; id / osm_id.

1. Check: a row matches an OSM POI by id, else by name (a variant equal or contained,
   5+ letters) and, when the row has coordinates, by distance < 300 m. Hours are compared
   as weekly schedules ("Mo-Su 10:00-22:00" == "ежедневно 10:00–22:00").
2. Add: a row with no OSM match becomes a manual POI `manual:poi/<row number>`,
   sources ["manual"], website = the official source; hours and address come only from
   the row. Rows without coordinates or without a source link are listed, not added.
The check runs against OSM data only, so manual POIs never inflate the "found" number.
"""

from __future__ import annotations

import argparse
import csv
import io
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import h3

from app.config import get_settings
from app.hours import ASTANA_TZ
from app.schemas import POI, TAG_VOCAB, Category
from pipeline.normalize import (
    _NON_WORD,
    contains_name,
    data_confidence,
    haversine_m,
    hours_flags,
    indoor_flag,
    name_keys,
    popularity,
    write_jsonl,
)

MATCH_RADIUS_M = 300.0
MANUAL_PREFIX = "manual:"
DAYS = ("mo", "tu", "we", "th", "fr", "sa", "su")
RU_DAYS = {"пн": "mo", "вт": "tu", "ср": "we", "чт": "th", "пт": "fr", "сб": "sa", "вс": "su"}
EVERY_DAY = ("ежедневно", "каждый день", "daily", "без выходных")
ROUND_THE_CLOCK = ("24/7", "круглосуточно")

COLUMNS: dict[str, tuple[str, ...]] = {
    "name": ("name", "название", "наименование", "место"),
    "hours": ("opening_hours", "hours", "часы", "часы работы", "режим работы", "время работы"),
    "lat": ("lat", "latitude", "широта"),
    "lon": ("lon", "lng", "longitude", "долгота"),
    "address": ("address", "адрес"),
    "source": ("source", "источник", "url", "ссылка", "website", "сайт", "официальный сайт"),
    "category": ("category", "категория", "тип"),
    "id": ("id", "osm_id", "poi_id"),
}

CATEGORY_WORDS: dict[str, Category] = {
    "музей": "museum", "галерея": "gallery", "арт": "gallery", "театр": "theatre",
    "кинотеатр": "cinema", "кино": "cinema", "библиотека": "library", "парк": "park",
    "сквер": "park", "сад": "park", "площадка": "playground", "смотровая": "viewpoint",
    "достопримечательность": "attraction", "аттракцион": "attraction",
    "памятник": "monument", "монумент": "monument", "мечеть": "religious",
    "церковь": "religious", "храм": "religious", "собор": "religious",
    "трц": "mall", "тц": "mall", "торговый": "mall", "кафе": "cafe", "кофейня": "cafe",
    "ресторан": "restaurant", "фастфуд": "fast_food", "бар": "bar", "паб": "bar",
    "клуб": "nightlife", "спорт": "sport", "каток": "sport", "бассейн": "sport",
    "развлечения": "entertainment", "квест": "entertainment", "боулинг": "entertainment",
}

Schedule = dict[str, frozenset[tuple[int, int]]]


@dataclass
class RowResult:
    row_no: int
    name: str
    poi: POI | None
    distance_m: float | None
    hours_status: str  # match | mismatch | no_ours | no_etalon | not_found
    etalon_hours: str
    our_hours: str


@dataclass
class EtalonOutcome:
    results: list[RowResult]
    added: list[POI] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)  # (name, reason)


# --------------------------------------------------------------------- reading


def read_rows(path: Path) -> list[dict[str, str]]:
    text = path.read_text(encoding="utf-8-sig")
    dialect = csv.Sniffer().sniff(text[:2048], delimiters=",;\t")
    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    mapping: dict[str, str] = {}
    for header in reader.fieldnames or []:
        key = header.strip().casefold()
        for name, aliases in COLUMNS.items():
            if key in aliases and name not in mapping:
                mapping[name] = header
    if "name" not in mapping:
        raise ValueError(f"etalon: no name column among {reader.fieldnames}")
    return [
        {name: (row.get(header) or "").strip() for name, header in mapping.items()}
        for row in reader
        if (row.get(mapping["name"]) or "").strip()
    ]


# ---------------------------------------------------------------------- hours


def _minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def _day_span(spec: str) -> list[str] | None:
    spec = spec.strip().casefold()
    for ru, en in RU_DAYS.items():
        spec = spec.replace(ru, en)
    days: list[str] = []
    for part in filter(None, (p.strip() for p in spec.split(","))):
        if "-" in part:
            a, b = (x.strip()[:2] for x in part.split("-", 1))
            if a not in DAYS or b not in DAYS:
                return None
            i, j = DAYS.index(a), DAYS.index(b)
            days += [DAYS[(i + k) % 7] for k in range((j - i) % 7 + 1)]
        elif part[:2] in DAYS:
            days.append(part[:2])
        else:
            return None
    return days


def parse_schedule(raw: str) -> Schedule | None:
    """Simple weekly schedules only; anything fancier (PH, months, "off") -> None."""
    text = raw.strip().casefold().replace("–", "-").replace("—", "-")
    if not text:
        return None
    if any(t in text for t in ROUND_THE_CLOCK) or text in {"00:00-24:00", "mo-su 00:00-24:00"}:
        return {d: frozenset({(0, 1440)}) for d in DAYS}
    for phrase in EVERY_DAY:
        text = text.replace(phrase, "mo-su")
    schedule: dict[str, set[tuple[int, int]]] = {d: set() for d in DAYS}
    for rule in filter(None, (r.strip() for r in text.split(";"))):
        times = re.findall(r"(\d{1,2}:\d{2})\s*-\s*(\d{1,2}:\d{2})", rule)
        if not times:
            return None
        head = rule[: rule.find(times[0][0])].strip().rstrip(":").strip()
        days = list(DAYS) if not head else _day_span(head)
        if days is None:
            return None
        for start, end in times:
            interval = (_minutes(start), _minutes(end) if end != "00:00" else 1440)
            for d in days:
                schedule[d].add(interval)
    return {d: frozenset(v) for d, v in schedule.items()}


def _hhmm(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def to_osm_hours(schedule: Schedule) -> str:
    """Weekly schedule -> OSM opening_hours ("Mo-Fr 09:00-18:00; Sa 10:00-16:00")."""
    if all(schedule[d] == frozenset({(0, 1440)}) for d in DAYS):
        return "24/7"
    rules: list[str] = []
    i = 0
    while i < len(DAYS):
        if not schedule[DAYS[i]]:
            i += 1
            continue
        j = i
        while j + 1 < len(DAYS) and schedule[DAYS[j + 1]] == schedule[DAYS[i]]:
            j += 1
        span = DAYS[i].title() if i == j else f"{DAYS[i].title()}-{DAYS[j].title()}"
        times = ",".join(f"{_hhmm(a)}-{_hhmm(b)}" for a, b in sorted(schedule[DAYS[i]]))
        rules.append(f"{span} {times}")
        i = j + 1
    return "; ".join(rules)


def compact_hours(raw: str) -> str:
    return re.sub(r"\s+", "", raw.casefold().replace("–", "-").replace("—", "-"))


def hours_status(etalon: str, ours: str | None) -> str:
    if not etalon:
        return "no_etalon"
    if not ours:
        return "no_ours"
    a, b = parse_schedule(etalon), parse_schedule(ours)
    if a is not None and b is not None:
        return "match" if a == b else "mismatch"
    return "match" if compact_hours(etalon) == compact_hours(ours) else "mismatch"


# ------------------------------------------------------------------- matching


def _float(value: str) -> float | None:
    try:
        return float(value.replace(",", "."))
    except ValueError:
        return None


def match_row(row: Mapping[str, str], pois: Sequence[POI]) -> tuple[POI | None, float | None]:
    if row.get("id"):
        wanted = row["id"] if ":" in row["id"] else f"osm:{row['id']}"
        for p in pois:
            if p.id == wanted:
                return p, None
    keys = {_NON_WORD.sub("", row["name"].casefold())}
    lat, lon = _float(row.get("lat", "")), _float(row.get("lon", ""))
    best: tuple[float, POI] | None = None
    for p in pois:
        if not contains_name(keys, name_keys(p)):
            continue
        if lat is not None and lon is not None:
            dist = haversine_m(lat, lon, p.geo.lat, p.geo.lon)
            if dist >= MATCH_RADIUS_M:
                continue
        else:
            dist = 0.0
        if best is None or dist < best[0]:
            best = (dist, p)
    if best is None:
        return None, None
    return best[1], (best[0] if lat is not None and lon is not None else None)


def compare(rows: Sequence[Mapping[str, str]], pois: Sequence[POI]) -> list[RowResult]:
    out: list[RowResult] = []
    for row_no, row in enumerate(rows, start=1):
        poi, dist = match_row(row, pois)
        etalon_hours = row.get("hours", "")
        status = "not_found" if poi is None else hours_status(etalon_hours, poi.opening_hours)
        out.append(
            RowResult(
                row_no=row_no,
                name=row["name"],
                poi=poi,
                distance_m=dist,
                hours_status=status,
                etalon_hours=etalon_hours,
                our_hours=(poi.opening_hours or "") if poi else "",
            )
        )
    return out


# ----------------------------------------------------------------- manual POIs


def category_of(raw: str) -> Category:
    text = raw.strip().casefold()
    if text in {"cafe", "restaurant", "fast_food", "bar", "nightlife", "museum", "gallery",
                "theatre", "cinema", "library", "park", "playground", "viewpoint",
                "attraction", "monument", "religious", "mall", "sport", "entertainment"}:
        return text  # type: ignore[return-value]
    for word in re.findall(r"\w+", text):
        if word in CATEGORY_WORDS:
            return CATEGORY_WORDS[word]
    return "other"


def manual_poi(row_no: int, row: Mapping[str, str], fetched_at: str) -> POI | str:
    """POI from an etalon row, or the reason it cannot be one."""
    lat, lon = _float(row.get("lat", "")), _float(row.get("lon", ""))
    source = row.get("source", "")
    if lat is None or lon is None:
        return "нет координат"
    if not source.startswith(("http://", "https://")):
        return "нет ссылки на официальный источник"
    category = category_of(row.get("category", ""))
    raw_hours = row.get("hours", "")
    schedule = parse_schedule(raw_hours) if raw_hours else None
    hours = to_osm_hours(schedule) if schedule else (raw_hours or None)
    address = row.get("address") or None
    tags = set(hours_flags(hours))
    indoor = indoor_flag(category, "")
    if indoor is True:
        tags.add("indoor")
    elif indoor is False:
        tags.add("outdoor")
    return POI(
        id=f"{MANUAL_PREFIX}poi/{row_no}",
        name=row["name"],
        name_i18n={"ru": row["name"]},
        category=category,
        tags=sorted(tags & TAG_VOCAB),
        geo={"lat": lat, "lon": lon},
        h3_r9=h3.latlng_to_cell(lat, lon, 9),
        address=address,
        opening_hours=hours,
        website=source,
        indoor=indoor,
        popularity=popularity({}, category),
        sources=["manual"],
        data_confidence=data_confidence(
            has_hours=bool(hours),
            has_contact=True,
            has_description=False,
            has_wikidata=False,
            has_address=bool(address),
            multilingual=False,
        ),
        fetched_at=fetched_at,
    )


def apply_etalon(
    pois: Sequence[POI], rows: Sequence[Mapping[str, str]], fetched_at: str
) -> tuple[list[POI], EtalonOutcome]:
    """Check against OSM POIs (old manual ones are dropped first), then add manual POIs."""
    osm_pois = [p for p in pois if not p.id.startswith(MANUAL_PREFIX)]
    outcome = EtalonOutcome(results=compare(rows, osm_pois))
    for res, row in zip(outcome.results, rows, strict=True):
        if res.poi is not None:
            continue
        made = manual_poi(res.row_no, row, fetched_at)
        if isinstance(made, str):
            outcome.skipped.append((row["name"], made))
        else:
            outcome.added.append(made)
    return sorted([*osm_pois, *outcome.added], key=lambda p: p.id), outcome


# --------------------------------------------------------------------- output


def headline(results: Sequence[RowResult]) -> str:
    total = len(results)
    found = sum(1 for r in results if r.poi is not None)
    c = Counter(r.hours_status for r in results)
    comparable = c["match"] + c["mismatch"]
    return (
        f"Сверка с эталоном (data/manual/etalon.csv, {total} мест): найдено в данных OSM "
        f"{found} из {total}; часы совпали у {c['match']} из {comparable}, где часы есть и "
        f"там и там (расходятся: {c['mismatch']}; у нас нет часов: {c['no_ours']})."
    )


def render(outcome: EtalonOutcome) -> str:
    labels = {
        "match": "совпали", "mismatch": "расходятся", "no_ours": "у нас нет часов",
        "no_etalon": "в эталоне нет часов", "not_found": "нет в OSM",
    }
    lines = [
        "# Сверка с эталоном",
        "",
        headline(outcome.results),
        "",
        f"Добавлено вручную: {len(outcome.added)}; не добавлено: {len(outcome.skipped)}.",
        "",
        "| № | Место (эталон) | Наш POI | Часы в эталоне | Часы у нас | Итог |",
        "|---:|---|---|---|---|---|",
    ]
    added_ids = {p.id for p in outcome.added}
    for r in outcome.results:
        ours = f"{r.poi.name} (`{r.poi.id}`)" if r.poi else "—"
        verdict = labels[r.hours_status]
        if r.poi is None and f"{MANUAL_PREFIX}poi/{r.row_no}" in added_ids:
            verdict += f", добавлено как `{MANUAL_PREFIX}poi/{r.row_no}`"
        lines.append(
            f"| {r.row_no} | {r.name} | {ours} | {r.etalon_hours or '—'} | "
            f"{r.our_hours or '—'} | {verdict} |"
        )
    if outcome.skipped:
        lines += ["", "Не добавлено:", *(f"- {name}: {why}" for name, why in outcome.skipped)]
    return "\n".join(lines) + "\n"


def etalon_path() -> Path:
    return get_settings().data_dir / "manual" / "etalon.csv"


def run_on(pois: Sequence[POI], path: Path | None = None) -> tuple[list[POI], list[str]]:
    """For run_all: returns POIs with manual ones added and notes for REPORT.md."""
    path = path or etalon_path()
    if not path.exists():
        return list(pois), []
    fetched_at = datetime.fromtimestamp(path.stat().st_mtime, ASTANA_TZ).date().isoformat()
    out, outcome = apply_etalon(pois, read_rows(path), fetched_at)
    (get_settings().poi_path.parent / "ETALON.md").write_text(render(outcome), encoding="utf-8")
    notes = [
        headline(outcome.results) + " Подробно: data/processed/ETALON.md.",
        f"Из эталона добавлено вручную (manual:poi, сайт-источник в website): "
        f"{len(outcome.added)}; без координат или ссылки не добавлено: {len(outcome.skipped)}.",
    ]
    return out, notes


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--etalon", type=Path, default=None)
    parser.add_argument("--area", choices=["astana", "zones", "fixture"], default="astana")
    args = parser.parse_args(argv)
    settings = get_settings()
    with settings.poi_path.open(encoding="utf-8") as fh:
        pois = [POI.model_validate_json(line) for line in fh if line.strip()]
    out, notes = run_on(pois, args.etalon)
    if not notes:
        print("no data/manual/etalon.csv yet")
        return
    write_jsonl(out, settings.poi_path)
    from pipeline.report import write_report

    write_report(args.area, notes=notes)
    for note in notes:
        print("NOTE", note)


if __name__ == "__main__":
    main()
