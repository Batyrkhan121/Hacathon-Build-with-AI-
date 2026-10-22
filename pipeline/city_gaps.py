"""«Карта пробелов данных»: data/processed/city_gaps.json (CityGaps, contract 1.4.1).

    python -m pipeline.city_gaps                     # poi.jsonl + walk graph -> city_gaps.json
    python -m pipeline.city_gaps --fetch-districts   # first refresh districts.json (OSM API)

Where Astana's open data lacks facts, per H3 r8 cell, per district and for the whole city, from
the places (data/processed/poi.jsonl) and the walk graph already built (walk_graph.npz + meta).
Counts only, as docs/ARCHITECTURE.md section 5 defines them (readers compute shares and «мало
данных»):
- places: all, without opening hours, wheelchair unknown, without a Kazakh name;
- walk graph: metres of edges, of them with neither lit (edge bit 2) nor unlit (bit 32);
- crossings (node bit 1): all, kind unknown (none of 2, 8, 32), kerb unknown (neither 4 nor 16).
A place belongs to the cell of its geo, a node to the cell of its point, an edge to the cell
of the midpoint between its ends. The metres of a cell are its rounded sum; the city is
exactly the sum over cells; empty cells are left out; cells go by h3_cell. The same input
gives the same file but for built_at. Aggregates of open data only (OSM, ODbL): no places,
no ids, no requests.

Districts (1.4.1) come from data/processed/districts.json (pipeline.districts: the official
list of the akimat and the OSM boundaries as drawn): a cell belongs to the district whose
outline holds its centre (h3.cell_to_latlng; even-odd rule; on the line itself, or in two
outlines, the smaller id). A district's counts are exactly the sum of its cells, the cells
in no outline are `unassigned`. Without districts.json the file has no districts block.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime

import h3
import numpy as np

from app.config import get_settings
from app.hours import ASTANA_TZ
from app.schemas import GAP_H3_RES, POI, CityGaps, GapCounts
from pipeline import districts as district_data
from pipeline.normalize import Edge
from pipeline.walk_coverage import crossing_class, kerb_class, lit_class
from pipeline.walk_graph import NODE_CROSSING, load_graph

log = logging.getLogger(__name__)

LIT = ("yes", "no", "unknown")
ON_LINE_DEG = 1e-9  # a centre this close to an outline edge is on the line


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


# ---------------------------------------------------------- points in outlines


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


def on_line_mask(lats: np.ndarray, lons: np.ndarray, edges: Sequence[Edge],
                 eps: float = ON_LINE_DEG) -> np.ndarray:
    """Points lying on an edge of the outline (within eps degrees)."""
    out = np.zeros(len(lats), dtype=bool)
    for (lat1, lon1), (lat2, lon2) in edges:
        near = ((np.minimum(lat1, lat2) - eps <= lats) & (lats <= np.maximum(lat1, lat2) + eps)
                & (np.minimum(lon1, lon2) - eps <= lons) & (lons <= np.maximum(lon1, lon2) + eps))
        if not near.any():
            continue
        dy, dx = lat2 - lat1, lon2 - lon1
        length = np.hypot(dy, dx)
        cross = np.abs((lats - lat1) * dx - (lons - lon1) * dy) / (length or 1.0)
        out |= near & (cross <= eps)
    return out


def assign_cells(cells: Sequence[str], outlines: Mapping[str, Sequence]) -> dict[str, str | None]:
    """{cell: district id or None}: the outline holding the cell centre; on the line, or in
    two outlines, the smaller id."""
    centres = [h3.cell_to_latlng(c) for c in cells]
    lats = np.array([c[0] for c in centres], dtype=np.float64)
    lons = np.array([c[1] for c in centres], dtype=np.float64)
    owner: list[str | None] = [None] * len(cells)
    for did in sorted(outlines, reverse=True):  # smaller ids last: they win
        edges = district_data.edges_of(outlines[did])
        hit = inside_mask(lats, lons, edges) | on_line_mask(lats, lons, edges)
        for i in np.flatnonzero(hit):
            owner[i] = did
    return dict(zip(cells, owner, strict=True))


# --------------------------------------------------------------------------- build


def newest_snapshot(meta: Mapping) -> str:
    """The newest OSM snapshot of the walk graph: osm_snapshots, else osm_snapshot."""
    dates = [d[:10] for d in (meta.get("osm_snapshots") or []) if d]
    return max(dates) if dates else str(meta["osm_snapshot"])[:10]


def _sum(cells: Sequence[Mapping]) -> dict[str, int]:
    return {name: sum(c[name] for c in cells) for name in GapCounts.model_fields}


def districts_block(cells: list[dict], districts_file: Mapping) -> dict:
    """GapDistricts from districts.json; sets "district" on the cells (dicts) in place."""
    entries = districts_file["districts"]
    changes = districts_file.get("composition_changes") or []
    status = {e["id"]: district_data.district_status(e, changes)[0] for e in entries}
    outlines = {e["id"]: e["outline"] for e in entries if status[e["id"]] != "missing"}
    owner = assign_cells([c["h3_cell"] for c in cells], outlines)
    for c in cells:
        c["district"] = owner[c["h3_cell"]]
    items = []
    for e in entries:
        mine = [c for c in cells if c["district"] == e["id"]]
        item = {"id": e["id"], "name": e["name"], "status": status[e["id"]],
                "cell_count": len(mine)}
        if status[e["id"]] != "missing":
            item.update(osm_relation=e["osm_relation"], boundary_date=e["boundary_date"],
                        counts=_sum(mine), outline=e["outline"])
        items.append(item)
    rest = [c for c in cells if c["district"] is None]
    return {
        "boundaries_date": districts_file["boundaries_date"],
        "official_list_url": districts_file.get("official_list_url"),
        "official_list_checked": districts_file.get("official_list_checked"),
        "items": items,
        "unassigned": _sum(rest),
        "unassigned_cells": len(rest),
    }


def build_city_gaps(graph: Mapping[str, np.ndarray], pois: Sequence[POI], meta: Mapping,
                    built_at: datetime, districts_file: Mapping | None = None) -> CityGaps:
    by_cell = tally(graph, pois, cells_of)
    cells = []
    for cell in sorted(by_cell):
        counts = by_cell[cell].counts()
        if counts["poi"] or counts["walk_m"] or counts["crossings"]:  # empty cells: left out
            cells.append({"h3_cell": cell, **counts})
    data = {
        "built_at": built_at,
        "poi_date": max(str(p.fetched_at)[:10] for p in pois),
        "walk_graph_date": newest_snapshot(meta),
        "city": _sum(cells),
    }
    if districts_file:
        data["districts"] = districts_block(cells, districts_file)
    data["cells"] = cells
    return CityGaps.model_validate(data)


def dump(gaps: CityGaps) -> str:
    """Readable head (an outline ring per line), one line per cell; the same CityGaps always
    gives the same text."""
    data = gaps.model_dump(mode="json")
    cells = data.pop("cells")
    head = district_data.compact_json(data)
    lines = ",\n".join(  # the cell id first: easier to read
        "    " + json.dumps({"h3_cell": c.pop("h3_cell"), **c}, ensure_ascii=False)
        for c in cells)
    return head[:-2] + ',\n  "cells": [\n' + lines + "\n  ]\n}\n"


def run(*, built_at: datetime | None = None, fetch_districts: bool = False) -> CityGaps:
    """Build and write settings.city_gaps_path from the files on disk (districts.json too)."""
    settings = get_settings()
    processed = settings.poi_path.parent
    if fetch_districts:
        district_data.refresh()
    graph_path = settings.walk_graph_path or processed / "walk_graph.npz"
    graph = load_graph(graph_path)
    meta = json.loads(graph_path.with_name("walk_graph_meta.json").read_text(encoding="utf-8"))
    with settings.poi_path.open(encoding="utf-8") as fh:
        pois = [POI.model_validate_json(line) for line in fh if line.strip()]
    gaps = build_city_gaps(graph, pois, meta,
                           built_at or datetime.now(ASTANA_TZ).replace(microsecond=0),
                           district_data.load())
    out = settings.city_gaps_path or processed / "city_gaps.json"
    tmp = out.with_name(out.name + ".tmp")  # next to it: the reader never sees half a file
    tmp.write_text(dump(gaps), encoding="utf-8")
    os.replace(tmp, out)
    return gaps


# ------------------------------------------------------------- the report check


def reconcile(city: Mapping[str, int], coverage: Mapping | None,
              pois: Sequence[POI]) -> list[dict]:
    """city against walk_coverage.json (REPORT's tables) and poi.jsonl, with the tolerances
    of docs/ARCHITECTURE.md: counts exactly, length 0.5 km, shares 0.1 p.p."""
    rows = [
        ("мест (строк poi.jsonl)", city["poi"], len(pois), 0),
        ("без часов работы", city["poi_no_hours"],
         sum(1 for p in pois if not (p.opening_hours or "").strip()), 0),
        ("wheelchair = unknown", city["poi_wheelchair_unknown"],
         sum(1 for p in pois if p.wheelchair == "unknown"), 0),
        ("без названия на казахском", city["poi_no_name_kk"],
         sum(1 for p in pois if not has_kk_name(p)), 0),
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


def districts_add_up(gaps: Mapping) -> bool:
    """Districts plus «вне районов» give exactly the city (all counters)."""
    block = gaps.get("districts")
    if not block:
        return True
    parts = [d["counts"] for d in block["items"] if d.get("counts")] + [block["unassigned"]]
    return all(sum(p[k] for p in parts) == gaps["city"][k] for k in GapCounts.model_fields)


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--fetch-districts", action="store_true",
                        help="refresh data/processed/districts.json from the OSM API first")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    gaps = run(fetch_districts=args.fetch_districts)
    summary = {"cells": len(gaps.cells), **gaps.city.model_dump()}
    if gaps.districts:
        summary["districts"] = {d.id: (d.status, d.cell_count) for d in gaps.districts.items}
        summary["unassigned_cells"] = gaps.districts.unassigned_cells
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()
