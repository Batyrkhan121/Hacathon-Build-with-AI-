"""Districts of Astana for the gaps map: the official list + OSM boundaries -> districts.json.

    python -m pipeline.city_gaps --fetch-districts   # refresh data/processed/districts.json

The list of districts is the official one: the akimat of Astana on gov.kz («Аппараты акимов
районов», OFFICIAL_LIST_URL), names in ru / kk / en as there, checked by a person on
OFFICIAL_LIST_CHECKED. Boundaries are the OSM administrative boundaries, fetched from the OSM
API (relation/full; Overpass was down when this was written): the city relation and the known
district relations are the seeds, and every admin relation that shares a boundary way with them
and lies inside the city is a candidate. A candidate matches an official district by name;
an OSM district inside the city that is not on the list is kept aside for REPORT.md.

data/processed/districts.json keeps what was fetched (outlines already simplified as the map
draws them, OSM names, last edit dates) and the list check, so the map builds without network.
The status of a district is decided when the map is built (district_status): no boundary in
OSM, or one that does not close or cannot be drawn within 10 m -> missing (the relation and
the reason are kept); a boundary whose name does not match the list, that has an equal rival,
or whose last edit is older than a change of the districts or their boundaries that touched it
(COMPOSITION_CHANGES, with their sources) -> stale; otherwise ok. Boundaries are never drawn
or fixed by hand. Data: OSM, ODbL 1.0.
"""

from __future__ import annotations

import json
import logging
import math
import re
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import date, datetime
from pathlib import Path

import httpx
import numpy as np

from app.config import get_settings
from app.hours import ASTANA_TZ
from app.schemas import GAP_MAX_OUTLINE_POINTS
from pipeline.fetch_osm import RETRY_PAUSES_S, user_agent

log = logging.getLogger(__name__)

OSM_API = "https://api.openstreetmap.org/api/0.6"
API_PAUSE_S = 1.0  # the OSM API usage policy: a light, steady pace
FORMAT_VERSION = 1

OFFICIAL_LIST_URL = "https://www.gov.kz/org/astana/about/structure/484/1?lang=ru"
OFFICIAL_LIST_CHECKED = "2026-10-22"
# id: (ru, kk, en), as the akimat names its district offices on gov.kz (lang=ru / kk / en)
OFFICIAL_DISTRICTS: dict[str, tuple[str, str, str]] = {
    "almaty": ("Алматы", "Алматы", "Almaty"),
    "baikonyr": ("Байқоңыр", "Байқоңыр", "Baikonyr"),
    "nura": ("Нұра", "Нұра", "Nura"),
    "saraishyk": ("Сарайшық", "Сарайшық", "Saraishyk"),
    "saryarka": ("Сарыарқа", "Сарыарқа", "Saryarka"),
    "yesil": ("Есиль", "Есіл", "Yesil"),
}
# changes of the district set: a boundary last edited before a change that touched its
# district is stale. "YYYY-MM" = the month is known, not the day.
COMPOSITION_CHANGES: list[dict] = [
    {"date": "2018-03-16", "districts": ["almaty", "baikonyr", "saryarka", "yesil"],
     "what": "границы районов «Алматы», «Байқоңыр», «Есиль», «Сарыарка» (постановление "
             "Правительства № 131)",
     "sources": ["https://adilet.zan.kz/rus/docs/P1800000131"]},
    {"date": "2023-01-19", "districts": ["nura", "yesil"],
     "what": "новому району присвоено название «Нұра» (совместный акт маслихата и акимата "
             "№ 315/39-VII, 511-89); район выделен из «Есиль»",
     "sources": ["https://adilet.zan.kz/rus/docs/V23AAZ01327",
                 "https://kazpravda.kz/n/o-granitsah-novogo-rayona-nura-rasskazali-v-akimate-stolitsy/"]},
    # The sources name only «Алматы» as the land of «Сарайшық» (its south, 6953 ha); the act
    # is titled «Об образовании нового района, установлении и изменении границ районов»,
    # so neighbours may have changed too, which the sources do not say: the 2025 and 2026
    # boundary changes below cover them anyway
    {"date": "2024-08", "districts": ["almaty", "saraishyk"],
     "what": "образован район «Сарайшық» (южная часть района «Алматы»)",
     "sources": ["https://www.gov.kz/memleket/entities/astana/press/news/details/1251104?lang=ru",
                 "https://bes.media/news/nazvanie-shestogo-rayona-v-astane-sarayshik-utverdili-v-maslihate/"]},
    # which districts the 2025 adjustment touched is not said: all six count as touched
    {"date": "2025-05-13", "districts": ["almaty", "baikonyr", "nura", "saraishyk", "saryarka",
                                         "yesil"],
     "what": "внеочередная сессия маслихата: корректировка границ между районами (изменения в "
             "совместное решение об образовании нового района и границах районов)",
     "sources": ["https://maslihat01.kz/ru/news/novosti/3063"]},
    {"date": "2026-08-14", "districts": ["almaty", "baikonyr", "nura", "saryarka", "yesil"],
     "what": "маслихат: «Об изменении и установлении границ некоторых районов города "
             "Астаны» — площади пяти районов уточнены, «Сарайшық» без изменений",
     "sources": ["https://maslihat01.kz/ru/news/anonsy/3544",
                 "https://www.vechastana.kz/news/razvitie-goroda-po-novym-pravilam",
                 "https://www.zakon.kz/obshestvo/6528093-v-astane-khotyat-izmenit-granitsy-pyati-rayonov.html"]},
]
CITY_RELATION = 3087155  # Astana, admin_level 4
# district relations found on 22.10.2026 (Wikidata P402 and shared boundary ways): the seeds
SEED_RELATIONS = (3482819, 8593081, 20593940, 19733918, 3486954, 3479876)
DISTRICT_LEVELS = ("5", "6", "7", "8", "9")
INSIDE_CITY_SHARE = 0.9  # a district of the city: most of its boundary inside the city
SIMPLIFY_M = 8.0  # + rounding to 5 decimals (<= 1.1 m): never coarser than 10 m
MAX_DEVIATION_M = 10.0  # checked on every fetched outline
DIGITS = 5

LatLon = tuple[float, float]
Ring = list[LatLon]


# ------------------------------------------------------------------- OSM geometry


def parse_full(xml: bytes | str) -> dict:
    """relation/full XML -> {"relations": {id: {...}}, "ways": {id: [...]}, "nodes": {...}}
    with tags, members, node refs, coordinates and timestamps."""
    root = ET.fromstring(xml)
    nodes = {int(n.get("id")): (float(n.get("lat")), float(n.get("lon")), n.get("timestamp"))
             for n in root.iter("node")}
    ways = {int(w.get("id")): {"refs": [int(nd.get("ref")) for nd in w.findall("nd")],
                               "timestamp": w.get("timestamp")} for w in root.iter("way")}
    relations = {}
    for r in root.iter("relation"):
        relations[int(r.get("id"))] = {
            "tags": {t.get("k"): t.get("v") for t in r.findall("tag")},
            "members": [(m.get("type"), int(m.get("ref")), m.get("role") or "")
                        for m in r.findall("member")],
            "timestamp": r.get("timestamp"),
        }
    return {"relations": relations, "ways": ways, "nodes": nodes}


def rings_of(full: Mapping, rid: int) -> list[Ring] | None:
    """Closed rings of a boundary relation (outer and inner ways joined end to end); None when
    the ways do not close (a broken boundary is not used)."""
    rel = full["relations"][rid]
    lines = []
    for kind, ref, role in rel["members"]:
        if kind != "way" or role not in ("outer", "inner", ""):
            continue
        refs = full["ways"].get(ref, {}).get("refs") or []
        if len(refs) >= 2 and all(r in full["nodes"] for r in refs):
            lines.append(list(refs))
    rings: list[list[int]] = []
    while lines:
        ring = lines.pop(0)
        while ring[0] != ring[-1]:
            for i, line in enumerate(lines):
                if line[0] == ring[-1]:
                    ring += line[1:]
                elif line[-1] == ring[-1]:
                    ring += line[-2::-1]
                elif line[-1] == ring[0]:
                    ring = line[:-1] + ring
                elif line[0] == ring[0]:
                    ring = line[:0:-1] + ring
                else:
                    continue
                lines.pop(i)
                break
            else:
                return None  # an open end: the boundary does not close
        rings.append(ring)
    if not rings:
        return None
    return [[full["nodes"][r][:2] for r in ring] for ring in rings]


def _member_points(full: Mapping, rid: int) -> Ring:
    """All points of the relation's ways, in no order (for a boundary that does not close)."""
    return [full["nodes"][r][:2] for kind, ref, _ in full["relations"][rid]["members"]
            if kind == "way" for r in full["ways"].get(ref, {}).get("refs", [])
            if r in full["nodes"]]


def last_edit(full: Mapping, rid: int) -> str:
    """The newest timestamp of the relation, its ways and their nodes: the boundary date."""
    rel = full["relations"][rid]
    stamps = [rel["timestamp"]]
    for kind, ref, _ in rel["members"]:
        way = full["ways"].get(ref) if kind == "way" else None
        if way:
            stamps.append(way["timestamp"])
            stamps += [full["nodes"][r][2] for r in way["refs"] if r in full["nodes"]]
    return max(s for s in stamps if s)[:10]


def _projector(points: Sequence[LatLon]) -> Callable[[Sequence[LatLon]], np.ndarray]:
    """Local metres around the mean latitude of `points` (the same for every ring of one
    comparison)."""
    k = 111_320.0 * math.cos(math.radians(sum(p[0] for p in points) / len(points)))
    return lambda ring: np.array([(lon * k, lat * 110_574.0) for lat, lon in ring])


def _segment_dist(pts: np.ndarray, a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """Distance of each point to the segment a-b (not to its line: a hairpin bend counts)."""
    seg = b - a
    length2 = float(seg @ seg)
    t = np.zeros(len(pts)) if not length2 else np.clip((pts - a) @ seg / length2, 0.0, 1.0)
    return np.hypot(*(pts - a - t[:, None] * seg).T)


def max_deviation_m(ring: Ring, simplified: Ring) -> float:
    """The farthest any point of `ring` lies from the simplified ring, metres."""
    project = _projector(ring)
    pts, out = project(ring), project(simplified)
    nearest = np.full(len(pts), np.inf)
    for a, b in zip(out, out[1:], strict=False):
        nearest = np.minimum(nearest, _segment_dist(pts, a, b))
    return float(nearest.max())


def _rounded(points: Iterable[LatLon]) -> Ring:
    out: Ring = []
    for lat, lon in points:
        point = (round(lat, DIGITS), round(lon, DIGITS))
        if not out or out[-1] != point:
            out.append(point)
    if out and out[0] != out[-1]:
        out.append(out[0])
    return out


def simplify(ring: Ring, tolerance_m: float = SIMPLIFY_M) -> Ring:
    """Douglas-Peucker on a closed ring in local metres (distance to the segment), then 5
    decimals; the ring stays closed. A ring that would shrink below 4 points (a small exclave)
    keeps its own rounded points instead; [] only when even those are fewer than 4."""
    pts = _projector(ring)(ring)
    keep = np.zeros(len(ring), dtype=bool)
    keep[0] = keep[-1] = True
    # split the closed ring at its farthest point from the start: two open paths
    far = int(np.argmax(np.hypot(*(pts - pts[0]).T)))
    keep[far] = True
    stack = [(0, far), (far, len(ring) - 1)]
    while stack:
        a, b = stack.pop()
        if b - a < 2:
            continue
        dist = _segment_dist(pts[a + 1:b], pts[a], pts[b])
        i = int(np.argmax(dist))
        if dist[i] > tolerance_m:
            keep[a + 1 + i] = True
            stack += [(a, a + 1 + i), (a + 1 + i, b)]
    out = _rounded(p for p, k in zip(ring, keep, strict=True) if k)
    if len(out) >= 4:
        return out
    whole = _rounded(ring)
    return whole if len(whole) >= 4 else []


def outline_of(rings: Sequence[Ring], max_m: float = MAX_DEVIATION_M) -> list[Ring]:
    """Simplified rings. Raises ValueError when the outline would be coarser than max_m or
    longer than GAP_MAX_OUTLINE_POINTS (never drawn rougher to fit)."""
    out = []
    for ring in rings:
        simple = simplify(ring)
        if not simple:
            log.warning("a ring of %d points is too small to keep (< 4 points)", len(ring))
            continue
        deviation = max_deviation_m(ring, simple)
        if deviation > max_m:
            raise ValueError(f"simplified ring is {deviation:.1f} m off the OSM line > {max_m} m")
        out.append(simple)
    total = sum(len(r) for r in out)
    if total > GAP_MAX_OUTLINE_POINTS:
        raise ValueError(f"outline has {total} points > {GAP_MAX_OUTLINE_POINTS} at 10 m")
    return out


# ------------------------------------------------------------------- names, status


def name_key(name: str) -> str:
    """«Есіл ауданы», "район «Есиль»", 'Yesil District' -> 'есіл' / 'есиль' / 'yesil'."""
    text = re.sub(r"[«»\"'“”]", " ", name.casefold())
    text = re.sub(r"\b(ауданы|аудан|район|district|ward)\b", " ", text)
    return " ".join(text.split())


def match_official(osm_tags: Mapping[str, str]) -> tuple[str | None, bool]:
    """(official id, the Kazakh name matches exactly). OSM `name` in Astana is Kazakh: an id
    found only through name:ru / name:en is a match whose name does not agree."""
    osm_kk = {name_key(osm_tags[k]) for k in ("name", "name:kk") if osm_tags.get(k)}
    others = {name_key(osm_tags[k]) for k in ("name:ru", "name:en") if osm_tags.get(k)}
    for did, (_ru, kk, _en) in OFFICIAL_DISTRICTS.items():
        if name_key(kk) in osm_kk:
            return did, True
    for did, (ru, _kk, en) in OFFICIAL_DISTRICTS.items():
        if {name_key(ru), name_key(en)} & (others | osm_kk):
            return did, False
    return None, False


def _change_end(day: str) -> date:
    """A change dated by month counts until that month ends (the day is not known)."""
    if len(day) == 7:
        y, m = int(day[:4]), int(day[5:7])
        return date(y + (m == 12), m % 12 + 1, 1)
    return date.fromisoformat(day)


def district_status(entry: Mapping, changes: Sequence[Mapping]) -> tuple[str, str]:
    """(ok | stale | missing, why) for one district of districts.json."""
    if not entry.get("osm_relation") or not entry.get("outline"):
        unusable = entry.get("unusable")
        if unusable:  # the relation exists, but its boundary cannot be used as it is
            return "missing", (f"граница в OSM есть (relation {unusable['osm_relation']}), "
                               f"но не используется: {unusable['why']}")
        return "missing", "границы нет в OSM"
    if not entry.get("name_matches"):
        osm = entry.get("osm_name") or {}
        return "stale", f"название в OSM не совпадает со списком: {osm.get('name')}"
    if entry.get("ambiguous_with"):
        others = ", ".join(str(r) for r in entry["ambiguous_with"])
        return "stale", (f"в OSM несколько равноправных границ района: взята relation "
                         f"{entry['osm_relation']}, есть ещё {others}")
    edited = date.fromisoformat(entry["boundary_date"])
    for change in changes:
        if entry["id"] in change["districts"] and edited < _change_end(change["date"]):
            return "stale", (f"граница в OSM ({entry['boundary_date']}) старше изменения "
                             f"состава или границ районов ({change['date']})")
    return "ok", "граница в OSM совпадает со списком"


# ------------------------------------------------------------------------- fetch


def _inside_share(rings: Sequence[Ring], city: Sequence[Ring]) -> float:
    """Share of the boundary points inside the city or on its line (a district shares
    much of its boundary with the city's: those points are on the line, not outside)."""
    from pipeline.city_gaps import inside_mask, on_line_mask

    points = [p for ring in rings for p in ring]
    if not points:
        return 0.0
    edges = [(a, b) for ring in city for a, b in zip(ring, ring[1:], strict=False)]
    lats, lons = np.array([p[0] for p in points]), np.array([p[1] for p in points])
    mask = inside_mask(lats, lons, edges) | on_line_mask(lats, lons, edges, eps=1e-7)
    return float(mask.mean())


def api_get(client: httpx.Client, path: str, pause_s: float = API_PAUSE_S,
            retry_pauses: Sequence[float] = RETRY_PAUSES_S,
            sleep: Callable[[float], None] = time.sleep) -> bytes:
    """GET OSM_API + path at a steady pace; 429, 5xx and network errors are retried after
    the pauses (CLAUDE.md), a refusal (403 and other 4xx) is not."""
    for attempt in range(len(retry_pauses) + 1):
        try:
            resp = client.get(OSM_API + path)
        except httpx.TransportError as exc:
            problem = type(exc).__name__
        else:
            if resp.status_code == 429 or resp.status_code >= 500:
                problem = f"HTTP {resp.status_code}"
            else:
                resp.raise_for_status()
                sleep(pause_s)
                return resp.content
        if attempt == len(retry_pauses):
            raise RuntimeError(f"OSM API {path}: {problem} after {attempt + 1} tries")
        log.warning("OSM API %s: %s, retry in %.0f s", path, problem, retry_pauses[attempt])
        sleep(retry_pauses[attempt])
    raise AssertionError("unreachable")


def fetch(get: Callable[[str], bytes] | None = None, pause_s: float = API_PAUSE_S) -> dict:
    """The districts.json content from the OSM API. `get(path)` returns the body of
    OSM_API + path (tests pass a fake)."""
    client = None
    if get is None:
        client = httpx.Client(timeout=120, headers={"User-Agent": user_agent()})

        def get(path: str) -> bytes:
            return api_get(client, path, pause_s)

    try:
        city_full = parse_full(get(f"/relation/{CITY_RELATION}/full"))
        city_rings = rings_of(city_full, CITY_RELATION)
        if not city_rings:
            raise ValueError("the city boundary does not close")
        fulls = {rid: parse_full(get(f"/relation/{rid}/full")) for rid in SEED_RELATIONS}
        # neighbours of the seeds and of the city: admin relations sharing a boundary way
        ways = {ref for full in [city_full, *fulls.values()] for rel in full["relations"].values()
                for kind, ref, _ in rel["members"] if kind == "way"}
        for wid in sorted(ways):
            for rel in ET.fromstring(get(f"/way/{wid}/relations")).iter("relation"):
                rid = int(rel.get("id"))
                tags = {t.get("k"): t.get("v") for t in rel.findall("tag")}
                if (rid not in fulls and tags.get("boundary") == "administrative"
                        and tags.get("admin_level") in DISTRICT_LEVELS):
                    fulls[rid] = parse_full(get(f"/relation/{rid}/full"))
    finally:
        if client is not None:
            client.close()
    return build_districts_file(city_rings, fulls)


def build_districts_file(city_rings: Sequence[Ring], fulls: Mapping[int, Mapping],
                         fetched_at: datetime | None = None) -> dict:
    """districts.json from the fetched relations: candidates inside the city, matched to the
    official list; the rest of the in-city districts aside, with the reason.

    Of several relations for one district the one with the exact Kazakh name, then the lower
    admin_level, then the smaller id is taken; a tie on name and level is kept as
    `ambiguous_with` (the district becomes stale). A relation whose boundary does not close or
    cannot be simplified within 10 m is not used: the district stays missing, with the
    relation and the reason in `unusable` (and in the log)."""
    when = fetched_at or datetime.now(ASTANA_TZ).replace(microsecond=0)
    candidates: dict[str, list[dict]] = {}
    unusable: dict[str, dict] = {}
    aside = []
    for rid in sorted(fulls):
        full = fulls[rid]
        rel = full["relations"].get(rid)
        if rel is None or rel["tags"].get("admin_level") not in DISTRICT_LEVELS:
            continue
        rings = rings_of(full, rid)
        # an open boundary has no rings: judge «inside the city» by its points
        shape = rings if rings is not None else [_member_points(full, rid)]
        if _inside_share(shape, city_rings) < INSIDE_CITY_SHARE:
            continue  # a district of the region around the city
        did, exact = match_official(rel["tags"])
        osm_name = {k: rel["tags"][k] for k in ("name", "name:ru", "name:kk", "name:en")
                    if k in rel["tags"]}
        why = None
        outline: list[Ring] = []
        if rings is None:
            why = "граница не замыкается"
        else:
            try:
                outline = outline_of(rings)
            except ValueError as exc:
                why = f"контур не упрощается в пределах 10 м и 4000 точек ({exc})"
        if why:
            log.warning("OSM relation %s (%s): %s", rid, osm_name.get("name"), why)
            if did is not None:
                unusable.setdefault(did, {"osm_relation": rid, "name": osm_name, "why": why})
            aside.append({"osm_relation": rid, "name": osm_name, "why": why})
            continue
        if did is None:
            aside.append({"osm_relation": rid, "name": osm_name, "why": "нет в официальном списке"})
            continue
        candidates.setdefault(did, []).append({
            "osm_relation": rid, "osm_name": osm_name, "name_matches": exact,
            "admin_level": int(rel["tags"]["admin_level"]),
            "boundary_date": last_edit(full, rid), "outline": outline})
    districts = []
    for did, (ru, kk, en) in OFFICIAL_DISTRICTS.items():
        entry: dict = {"id": did, "name": {"ru": ru, "kk": kk, "en": en}}
        found = sorted(candidates.get(did, []),
                       key=lambda c: (not c["name_matches"], c["admin_level"], c["osm_relation"]))
        if found:
            best = found[0]
            entry.update({k: v for k, v in best.items() if k != "admin_level"})
            ties = [c["osm_relation"] for c in found[1:]
                    if (c["name_matches"], c["admin_level"])
                    == (best["name_matches"], best["admin_level"])]
            if ties:
                entry["ambiguous_with"] = ties
            aside += [{"osm_relation": c["osm_relation"], "name": c["osm_name"],
                       "why": f"ещё одна граница района «{ru}» (взята {best['osm_relation']})"}
                      for c in found[1:]]
        else:
            entry.update({"osm_relation": None, "outline": []})
            if did in unusable:
                entry["unusable"] = unusable[did]
        districts.append(entry)
    return {
        "format_version": FORMAT_VERSION,
        "fetched_at": when.isoformat(),
        "boundaries_date": when.date().isoformat(),  # the OSM API gives the live data
        "boundaries_source": f"{OSM_API} relation/full",
        "city_relation": CITY_RELATION,
        "official_list_url": OFFICIAL_LIST_URL,
        "official_list_checked": OFFICIAL_LIST_CHECKED,
        "composition_changes": COMPOSITION_CHANGES,
        "districts": districts,
        "osm_not_in_list": aside,
    }


def compact_json(data: object) -> str:
    """json.dumps(indent=2), but each ring of [lat, lon] points on one line."""
    rings: list[str] = []

    def mark(value: object) -> object:
        if (isinstance(value, list) and value and all(
                isinstance(p, list | tuple) and len(p) == 2
                and all(isinstance(x, int | float) for x in p) for p in value)):
            rings.append(json.dumps([list(p) for p in value]))
            return f"@@ring{len(rings) - 1}@@"
        if isinstance(value, dict):
            return {k: mark(v) for k, v in value.items()}
        if isinstance(value, list):
            return [mark(v) for v in value]
        return value

    text = json.dumps(mark(data), ensure_ascii=False, indent=2)
    return re.sub(r'"@@ring(\d+)@@"', lambda m: rings[int(m.group(1))], text)


def dump(data: Mapping) -> str:
    return compact_json(data) + "\n"


def districts_path() -> Path:
    return get_settings().poi_path.parent / "districts.json"


def load(path: Path | None = None) -> dict | None:
    path = path or districts_path()
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def refresh(path: Path | None = None) -> dict:
    data = fetch()
    path = path or districts_path()
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(dump(data), encoding="utf-8")
    tmp.replace(path)
    return data


def edges_of(outline: Iterable[Sequence[Sequence[float]]]) -> list[tuple[LatLon, LatLon]]:
    return [((a[0], a[1]), (b[0], b[1])) for ring in outline
            for a, b in zip(ring, ring[1:], strict=False)]
