"""«Карта пробелов данных»: data/processed/city_gaps.json (CityGaps, contract 1.4.0).

    python -m pipeline.city_gaps                # poi.jsonl + walk graph -> city_gaps.json
    python -m pipeline.city_gaps --fetch-districts   # also district boundaries for REPORT

Where Astana's open data lacks facts, per H3 r8 cell and for the whole city, from the places
(data/processed/poi.jsonl) and the walk graph already built (walk_graph.npz + meta). Counts
only, as docs/ARCHITECTURE.md section 5 defines them (readers compute shares and «мало
данных»):
- places: all, without opening hours, wheelchair unknown, without a Kazakh name;
- walk graph: metres of edges, of them with neither lit (edge bit 2) nor unlit (bit 32);
- crossings (node bit 1): all, kind unknown (none of 2, 8, 32), kerb unknown (neither 4 nor 16).
A place belongs to the cell of its geo, a node to the cell of its point, an edge to the cell
of the midpoint between its ends. The metres of a cell are its rounded sum; the city is
exactly the sum over cells; empty cells are left out; cells go by h3_cell. The same input
gives the same file but for built_at. Aggregates of open data only (OSM, ODbL): no places,
no ids, no requests.

REPORT.md also gets a district table (the full «есть / нет / нет данных» split), when OSM
boundaries of the city districts are on disk (--fetch-districts, Overpass); without them it
says so. District rows describe gaps in open data, not the districts themselves.
"""

from __future__ import annotations

import argparse
import json
import logging
import time
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import h3
import numpy as np

from app.config import get_settings
from app.hours import ASTANA_TZ
from app.schemas import GAP_H3_RES, POI, CityGaps, GapCell, GapCounts
from pipeline.fetch_osm import (
    ASTANA_BBOX,
    OverpassError,
    is_fresh,
    raw_path,
    run_query,
    save_json,
)
from pipeline.normalize import Edge
from pipeline.walk_coverage import crossing_class, kerb_class, lit_class
from pipeline.walk_graph import NODE_CROSSING, load_graph

log = logging.getLogger(__name__)

DISTRICTS_CHUNK = "astana_districts"
# admin_level of city districts differs between countries: levels 5-9 are fetched and the
# largest units (the lowest level) lying inside the city are taken. Level 4 and above (the
# city itself, the region, the country) are never districts
DISTRICT_LEVELS = ("5", "6", "7", "8", "9")
# a district of Astana lies inside the city: most of its boundary within the city bbox
# (the Akmola region districts around it reach far outside)
DISTRICT_INSIDE_SHARE = 0.9
BBOX_MARGIN_DEG = 0.05
OUTSIDE = "вне районов"

LIT = ("yes", "no", "unknown")


# -------------------------------------------------------------------------- tally


@dataclass
class Tally:
    """What one area holds, with the full split (the file keeps only totals and «no data»)."""

    lit_m: dict[str, float] = field(default_factory=lambda: dict.fromkeys(LIT, 0.0))
    crossings: Counter = field(default_factory=Counter)  # signals / marked / unmarked / unknown
    kerbs: Counter = field(default_factory=Counter)  # lowered / raised / unknown
    wheelchair: Counter = field(default_factory=Counter)  # yes / limited / no / unknown
    hours: Counter = field(default_factory=Counter)  # known / unknown
    name_kk: Counter = field(default_factory=Counter)  # yes / no

    def __iadd__(self, other: Tally) -> Tally:
        for k, v in other.lit_m.items():
            self.lit_m[k] += v
        for name in ("crossings", "kerbs", "wheelchair", "hours", "name_kk"):
            getattr(self, name).update(getattr(other, name))
        return self

    def counts(self) -> dict[str, int]:
        """GapCounts fields; metres rounded here, once per area."""
        return {
            "poi": sum(self.wheelchair.values()),
            "poi_no_hours": self.hours["unknown"],
            "poi_wheelchair_unknown": self.wheelchair["unknown"],
            "poi_no_name_kk": self.name_kk["no"],
            "walk_m": round(sum(self.lit_m.values())),
            "walk_lit_unknown_m": round(self.lit_m["unknown"]),
            "crossings": sum(self.crossings.values()),
            "crossings_kind_unknown": self.crossings["unknown"],
            "crossings_kerb_unknown": self.kerbs["unknown"],
        }


def has_kk_name(poi: POI) -> bool:
    return bool((poi.name_i18n.get("kk") or "").strip())


def tally(graph: Mapping[str, np.ndarray], pois: Sequence[POI],
          area_of: Callable[[np.ndarray, np.ndarray], Sequence[str]]) -> dict[str, Tally]:
    """{area: Tally}. `area_of(lats, lons)` names the area of each point."""
    lat = graph["node_lat"].astype(np.float64)
    lon = graph["node_lon"].astype(np.float64)
    u, v = graph["edge_u"], graph["edge_v"]
    out: dict[str, Tally] = {}

    def at(key: str) -> Tally:
        return out.setdefault(key, Tally())

    if len(u):  # an edge by the midpoint of its two ends
        for key, length, flags in zip(area_of((lat[u] + lat[v]) / 2, (lon[u] + lon[v]) / 2),
                                      graph["edge_len_m"].astype(np.float64),
                                      graph["edge_flags"], strict=True):
            at(key).lit_m[lit_class(int(flags))] += float(length)
    crossing = np.flatnonzero(graph["node_flags"] & NODE_CROSSING)
    if len(crossing):
        for key, flags in zip(area_of(lat[crossing], lon[crossing]),
                              graph["node_flags"][crossing], strict=True):
            at(key).crossings[crossing_class(int(flags))] += 1
            at(key).kerbs[kerb_class(int(flags))] += 1
    if pois:
        keys = area_of(np.array([p.geo.lat for p in pois]), np.array([p.geo.lon for p in pois]))
        for key, p in zip(keys, pois, strict=True):
            t = at(key)
            t.wheelchair[p.wheelchair] += 1
            t.hours["known" if (p.opening_hours or "").strip() else "unknown"] += 1
            t.name_kk["yes" if has_kk_name(p) else "no"] += 1
    return out


def cells_of(lats: np.ndarray, lons: np.ndarray, res: int = GAP_H3_RES) -> list[str]:
    return [h3.latlng_to_cell(float(a), float(b), res) for a, b in zip(lats, lons, strict=True)]


def total(tallies: Mapping[str, Tally]) -> Tally:
    out = Tally()
    for t in tallies.values():
        out += t
    return out


# --------------------------------------------------------------------------- build


def newest_snapshot(meta: Mapping) -> str:
    """The newest OSM snapshot of the walk graph: osm_snapshots, else osm_snapshot."""
    dates = [d[:10] for d in (meta.get("osm_snapshots") or []) if d]
    return max(dates) if dates else str(meta["osm_snapshot"])[:10]


def build_city_gaps(graph: Mapping[str, np.ndarray], pois: Sequence[POI], meta: Mapping,
                    built_at: datetime) -> CityGaps:
    by_cell = tally(graph, pois, cells_of)
    cells = []
    for cell in sorted(by_cell):
        counts = by_cell[cell].counts()
        if counts["poi"] or counts["walk_m"] or counts["crossings"]:  # empty cells: left out
            cells.append(GapCell(h3_cell=cell, **counts))
    city = {name: sum(getattr(c, name) for c in cells) for name in GapCounts.model_fields}
    return CityGaps.model_validate({
        "built_at": built_at,
        "poi_date": max(str(p.fetched_at)[:10] for p in pois),
        "walk_graph_date": newest_snapshot(meta),
        "city": city,
        "cells": [c.model_dump() for c in cells],
    })


def dump(gaps: CityGaps) -> str:
    """Readable head, one line per cell; the same CityGaps always gives the same text."""
    data = gaps.model_dump(mode="json")
    cells = data.pop("cells")
    head = json.dumps(data, ensure_ascii=False, indent=2)
    lines = ",\n".join(  # the cell id first: easier to read
        "    " + json.dumps({"h3_cell": c.pop("h3_cell"), **c}, ensure_ascii=False)
        for c in cells)
    return head[:-2] + ',\n  "cells": [\n' + lines + "\n  ]\n}\n"


def run(*, built_at: datetime | None = None, fetch_districts_too: bool = False) -> CityGaps:
    """Build and write settings.city_gaps_path from the files on disk."""
    settings = get_settings()
    processed = settings.poi_path.parent
    graph_path = settings.walk_graph_path or processed / "walk_graph.npz"
    graph = load_graph(graph_path)
    meta = json.loads(graph_path.with_name("walk_graph_meta.json").read_text(encoding="utf-8"))
    with settings.poi_path.open(encoding="utf-8") as fh:
        pois = [POI.model_validate_json(line) for line in fh if line.strip()]
    gaps = build_city_gaps(graph, pois, meta,
                           built_at or datetime.now(ASTANA_TZ).replace(microsecond=0))
    out = settings.city_gaps_path or processed / "city_gaps.json"
    out.write_text(dump(gaps), encoding="utf-8")
    if fetch_districts_too:
        fetch_districts(raw_dir=settings.data_dir / "raw")
    return gaps


# ------------------------------------------------------------- the report check


def reconcile(city: Mapping[str, int], coverage: Mapping | None,
              poi_lines: int, wheelchair_unknown: int) -> list[dict]:
    """city against walk_coverage.json (REPORT's tables) and poi.jsonl, with the tolerances
    of docs/ARCHITECTURE.md: counts exactly, length 0.5 km, shares 0.1 p.p."""
    rows = [
        ("мест (строк poi.jsonl)", city["poi"], poi_lines, 0),
        ("wheelchair = unknown", city["poi_wheelchair_unknown"], wheelchair_unknown, 0),
    ]
    if coverage:
        walk_km = city["walk_m"] / 1000
        unknown_share = city["walk_lit_unknown_m"] / city["walk_m"] if city["walk_m"] else 0.0
        rows += [
            ("переходов (crossings_total)", city["crossings"], coverage["crossings_total"], 0),
            ("тип перехода не указан", city["crossings_kind_unknown"],
             coverage["crossings"]["unknown"]["nodes"], 0),
            ("бордюр: нет данных", city["crossings_kerb_unknown"],
             coverage["kerbs"]["unknown"]["nodes"], 0),
            ("длина путей, км", round(walk_km, 1), coverage["length_km"], 0.5),
            ("освещение: нет данных, % длины", round(100 * unknown_share, 2),
             round(100 * coverage["lit"]["unknown"]["length_share"], 2), 0.1),
        ]
    return [{"what": what, "map": ours, "report": theirs,
             "ok": abs(ours - theirs) <= tol + 1e-9} for what, ours, theirs, tol in rows]


# ---------------------------------------------------------------------- districts


@dataclass
class District:
    name: str
    osm_id: str
    edges: list[Edge]
    admin_level: int = 6


def districts_query(bbox: tuple[float, float, float, float] = ASTANA_BBOX) -> str:
    s, w, n, e = bbox
    levels = "|".join(DISTRICT_LEVELS)
    return (f"[out:json][timeout:120];\n"
            f'relation["boundary"="administrative"]["admin_level"~"^({levels})$"]'
            f"({s},{w},{n},{e});\nout geom;")


def fetch_districts(
    *, force: bool = False, raw_dir: Path | None = None, query_fn: Callable[..., dict] = run_query
) -> Path | None:
    """District boundaries (`out geom`) -> data/raw/osm_astana_districts.json. Returns None
    when Overpass is down and nothing is cached."""
    path = raw_path(DISTRICTS_CHUNK, raw_dir)
    if not force and is_fresh(path, ttl_s=30 * 86400):  # boundaries rarely move
        return path
    try:
        payload = query_fn(districts_query(), check_status=True)
    except OverpassError as exc:
        log.error("district boundaries failed: %s", exc)
        return path if path.exists() else None
    payload.setdefault("qadam", {}).update(
        {"chunk": DISTRICTS_CHUNK, "fetched_at_unix": int(time.time())}
    )
    save_json(path, payload)
    return path


def districts_from(payload: Mapping,
                   bbox: tuple[float, float, float, float] = ASTANA_BBOX) -> list[District]:
    """The districts of the city from an `out geom` answer: of the boundaries lying inside the
    city, those of the lowest admin_level. Member ways are not joined into rings: the
    even-odd test only needs the boundary edges."""
    s, w, n, e = bbox
    s, w, n, e = s - BBOX_MARGIN_DEG, w - BBOX_MARGIN_DEG, n + BBOX_MARGIN_DEG, e + BBOX_MARGIN_DEG
    found: list[District] = []
    for el in payload.get("elements", []):
        tags = el.get("tags") or {}
        if (el.get("type") != "relation" or tags.get("boundary") != "administrative"
                or tags.get("admin_level") not in DISTRICT_LEVELS):
            continue
        name = tags.get("name:ru") or tags.get("name")
        lines = [m.get("geometry") or [] for m in el.get("members", [])
                 if m.get("type") == "way" and m.get("role") in ("outer", "inner", "")]
        edges: list[Edge] = [((a["lat"], a["lon"]), (b["lat"], b["lon"]))
                             for line in lines for a, b in zip(line, line[1:], strict=False)]
        points = [pt for line in lines for pt in line]
        if not name or not edges:
            continue
        inside = sum(1 for pt in points if s <= pt["lat"] <= n and w <= pt["lon"] <= e)
        if inside / len(points) >= DISTRICT_INSIDE_SHARE:
            found.append(District(name, f"osm:relation/{el['id']}", edges,
                                  int(tags["admin_level"])))
    top = min((d.admin_level for d in found), default=None)
    return sorted((d for d in found if d.admin_level == top), key=lambda d: d.name)


def inside_mask(lats: np.ndarray, lons: np.ndarray, edges: Sequence[Edge]) -> np.ndarray:
    """normalize.inside for many points at once: the points are sorted by latitude, and each
    boundary edge only looks at the band of points between its two latitudes."""
    order = np.argsort(lats, kind="stable")
    ys, xs = lats[order], lons[order]
    odd = np.zeros(len(ys), dtype=bool)
    for (lat1, lon1), (lat2, lon2) in edges:
        if lat1 == lat2:
            continue
        lo, hi = (lat1, lat2) if lat1 < lat2 else (lat2, lat1)
        a, b = np.searchsorted(ys, lo, side="left"), np.searchsorted(ys, hi, side="left")
        if a == b:
            continue  # same rule as inside(): lo <= lat < hi crosses the edge's line
        x_at = lon1 + (ys[a:b] - lat1) * (lon2 - lon1) / (lat2 - lat1)
        odd[a:b] ^= xs[a:b] < x_at
    mask = np.zeros(len(ys), dtype=bool)
    mask[order] = odd
    return mask


def district_names(lats: np.ndarray, lons: np.ndarray,
                   districts: Sequence[District]) -> list[str]:
    names = np.full(len(lats), OUTSIDE, dtype=object)
    free = np.ones(len(lats), dtype=bool)
    for d in districts:
        hit = inside_mask(lats, lons, d.edges) & free
        names[hit] = d.name
        free &= ~hit
    return names.tolist()


def district_tallies(raw_dir: Path) -> tuple[list[District], dict[str, Tally]] | None:
    """For REPORT.md: tallies per district from the boundaries on disk, None without them."""
    path = raw_path(DISTRICTS_CHUNK, raw_dir)
    if not path.exists():
        return None
    districts = districts_from(json.loads(path.read_text(encoding="utf-8")))
    if not districts:
        return None
    settings = get_settings()
    processed = settings.poi_path.parent
    graph = load_graph(settings.walk_graph_path or processed / "walk_graph.npz")
    with settings.poi_path.open(encoding="utf-8") as fh:
        pois = [POI.model_validate_json(line) for line in fh if line.strip()]
    return districts, tally(graph, pois, lambda la, lo: district_names(la, lo, districts))


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--fetch-districts", action="store_true",
                        help="also fetch district boundaries (Overpass) for REPORT.md")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    gaps = run(fetch_districts_too=args.fetch_districts)
    print(json.dumps({"cells": len(gaps.cells), **gaps.city.model_dump()}, ensure_ascii=False))


if __name__ == "__main__":
    main()
