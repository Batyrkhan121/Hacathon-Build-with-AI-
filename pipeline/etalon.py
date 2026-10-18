"""Check our POIs against a hand-made reference list (data/manual/etalon.csv).

    python -m pipeline.etalon

The CSV comes from the team (role A): real places with their hours checked by hand.
Columns are matched by name, case-insensitively (any order, `,` or `;` delimiter):
name / название; opening_hours / часы / часы работы / режим работы; optional lat / широта,
lon / долгота, address / адрес, id / osm_id.
A row matches a POI by id, else by name (a variant equal or contained, 5+ letters) and,
when the row has coordinates, by distance < 300 m. Hours are compared as weekly
schedules ("Mo-Su 10:00-22:00" == "ежедневно 10:00–22:00"), else as normalized text.
Writes data/processed/ETALON.md and returns the headline for REPORT.md.
"""

from __future__ import annotations

import argparse
import csv
import io
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from app.config import get_settings
from app.schemas import POI
from pipeline.normalize import _NON_WORD, contains_name, haversine_m, name_keys

MATCH_RADIUS_M = 300.0
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
    "id": ("id", "osm_id", "poi_id"),
}

Schedule = dict[str, frozenset[tuple[int, int]]]


@dataclass
class RowResult:
    name: str
    poi: POI | None
    distance_m: float | None
    hours_status: str  # match | mismatch | no_ours | no_etalon | not_found
    etalon_hours: str
    our_hours: str


# --------------------------------------------------------------------- reading


def read_rows(path: Path) -> list[dict[str, str]]:
    text = path.read_text(encoding="utf-8-sig")
    dialect = csv.Sniffer().sniff(text[:2048], delimiters=",;\t")
    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    mapping: dict[str, str] = {}
    for header in reader.fieldnames or []:
        key = header.strip().casefold()
        for field, aliases in COLUMNS.items():
            if key in aliases and field not in mapping:
                mapping[field] = header
    if "name" not in mapping:
        raise ValueError(f"etalon: no name column among {reader.fieldnames}")
    return [
        {field: (row.get(header) or "").strip() for field, header in mapping.items()}
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
        wanted = row["id"] if row["id"].startswith("osm:") else f"osm:{row['id']}"
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
    for row in rows:
        poi, dist = match_row(row, pois)
        etalon_hours = row.get("hours", "")
        status = "not_found" if poi is None else hours_status(etalon_hours, poi.opening_hours)
        out.append(
            RowResult(
                name=row["name"],
                poi=poi,
                distance_m=dist,
                hours_status=status,
                etalon_hours=row.get("hours", ""),
                our_hours=(poi.opening_hours or "") if poi else "",
            )
        )
    return out


def headline(results: Sequence[RowResult]) -> str:
    total = len(results)
    found = sum(1 for r in results if r.poi is not None)
    c = Counter(r.hours_status for r in results)
    comparable = c["match"] + c["mismatch"]
    return (
        f"Сверка с эталоном (data/manual/etalon.csv, {total} мест): найдено в наших данных "
        f"{found} из {total}; часы совпали у {c['match']} из {comparable}, где часы есть и "
        f"там и там (расходятся: {c['mismatch']}; у нас нет часов: {c['no_ours']})."
    )


def render(results: Sequence[RowResult]) -> str:
    labels = {
        "match": "совпали", "mismatch": "расходятся", "no_ours": "у нас нет часов",
        "no_etalon": "в эталоне нет часов", "not_found": "не найдено",
    }
    lines = [
        "# Сверка с эталоном",
        "",
        headline(results),
        "",
        "| Место (эталон) | Наш POI | Часы в эталоне | Часы у нас | Итог |",
        "|---|---|---|---|---|",
    ]
    for r in results:
        ours = f"{r.poi.name} (`{r.poi.id}`)" if r.poi else "—"
        lines.append(
            f"| {r.name} | {ours} | {r.etalon_hours or '—'} | {r.our_hours or '—'} | "
            f"{labels[r.hours_status]} |"
        )
    return "\n".join(lines) + "\n"


def run(etalon_path: Path | None = None) -> str | None:
    """Writes data/processed/ETALON.md; returns the headline, or None without an etalon."""
    settings = get_settings()
    path = etalon_path or settings.data_dir / "manual" / "etalon.csv"
    if not path.exists():
        return None
    with settings.poi_path.open(encoding="utf-8") as fh:
        pois = [POI.model_validate_json(line) for line in fh if line.strip()]
    results = compare(read_rows(path), pois)
    (settings.poi_path.parent / "ETALON.md").write_text(render(results), encoding="utf-8")
    return headline(results)


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--etalon", type=Path, default=None)
    args = parser.parse_args(argv)
    result = run(args.etalon)
    print(result or "no data/manual/etalon.csv yet")


if __name__ == "__main__":
    main()
