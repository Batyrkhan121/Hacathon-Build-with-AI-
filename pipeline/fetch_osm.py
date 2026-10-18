"""OSM Overpass -> data/raw/osm_<chunk>.json, one cached file per chunk.

Areas are split into chunks so a flaky Overpass costs one chunk, not the whole city:
- `fixture`: EXPO + Mega Silk Way, 1.5 km (T10 test fixture);
- `zones`: demo zones, 2 km around named landmarks (fetched first, small and fast);
- `astana`: the city bbox cut into a grid of tiles.

A chunk file younger than 24 h is reused, so a rerun downloads only what is missing.
Before each request the mirror's /api/status is checked; requests are spaced by a pause.
Mirrors are tried in turn on network errors, 429 and 5xx; up to 3 rounds with 5 -> 15 -> 45 s.

    python -m pipeline.fetch_osm --area zones
    python -m pipeline.fetch_osm --area astana [--force]
"""

from __future__ import annotations

import argparse
import json
import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

import httpx

from app.config import get_settings

log = logging.getLogger(__name__)

ENDPOINTS: tuple[str, ...] = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
)
RETRY_PAUSES_S: tuple[float, ...] = (5.0, 15.0, 45.0)
BETWEEN_CHUNKS_S = 5.0
ROUND_PAUSE_S = 60.0
TIMEOUT_S = 180.0
STATUS_TIMEOUT_S = 10.0
CACHE_TTL_S = 24 * 3600

# south, west, north, east
ASTANA_BBOX: tuple[float, float, float, float] = (51.03, 71.20, 51.27, 71.65)
ASTANA_GRID: tuple[int, int] = (3, 3)  # rows x cols -> 9 tiles of ~9 x 10 km

FIXTURE_CENTERS: tuple[tuple[float, float], ...] = ((51.0894, 71.4161), (51.0889, 71.4103))
FIXTURE_RADIUS_M = 1500

ZONE_RADIUS_M = 2000
# demo zones: centres are OSM-verified landmark points (data/landmarks.json); Ak Orda is
# approximate, its 2 km circle overlaps the Pyramid one anyway
ZONES: dict[str, tuple[tuple[float, float], ...]] = {
    "expo_mega": ((51.08925, 71.41619), (51.08909, 71.40733)),
    "baiterek_khan_shatyr": ((51.12829, 71.43045), (51.13251, 71.40387)),
    "akorda_pyramid_hazrat": ((51.1258, 71.4464), (51.12312, 71.46346), (51.12543, 71.47219)),
    "opera_nurly_zhol": ((51.13545, 71.41075), (51.11201, 71.52887)),
    "airport_nqz": ((51.02797, 71.46348),),
}

SELECTORS: tuple[str, ...] = (
    '["amenity"~"^(cafe|restaurant|fast_food|bar|pub|ice_cream|food_court|theatre|cinema'
    '|arts_centre|library|nightclub|place_of_worship|coworking_space)$"]',
    '["tourism"~"^(museum|gallery|attraction|viewpoint|artwork|theme_park|zoo|aquarium)$"]',
    '["leisure"~"^(park|garden|playground|water_park|sports_centre|ice_rink|amusement_arcade'
    '|escape_game|bowling_alley|trampoline_park|swimming_pool)$"]',
    '["shop"="mall"]',
    '["historic"~"^(monument|memorial|building|castle)$"]',
)


class OverpassError(RuntimeError):
    """All mirrors and retries failed."""


@dataclass(frozen=True)
class Chunk:
    name: str  # file stem: data/raw/osm_<name>.json
    filters: tuple[str, ...]  # Overpass spatial filters without parentheses


def around(radius_m: int, centers: Sequence[tuple[float, float]]) -> tuple[str, ...]:
    return tuple(f"around:{radius_m},{lat},{lon}" for lat, lon in centers)


def bbox_tiles(
    bbox: tuple[float, float, float, float], rows: int, cols: int
) -> list[tuple[float, float, float, float]]:
    s, w, n, e = bbox
    dlat, dlon = (n - s) / rows, (e - w) / cols
    return [
        (
            round(s + r * dlat, 5),
            round(w + c * dlon, 5),
            round(s + (r + 1) * dlat, 5),
            round(w + (c + 1) * dlon, 5),
        )
        for r in range(rows)
        for c in range(cols)
    ]


def area_chunks(area: str) -> list[Chunk]:
    if area == "fixture":
        return [Chunk("fixture", around(FIXTURE_RADIUS_M, FIXTURE_CENTERS))]
    if area == "zones":
        return [Chunk(f"zone_{name}", around(ZONE_RADIUS_M, c)) for name, c in ZONES.items()]
    if area == "astana":
        tiles = bbox_tiles(ASTANA_BBOX, *ASTANA_GRID)
        return [
            Chunk(f"astana_tile_{i}", (f"{s},{w},{n},{e}",))
            for i, (s, w, n, e) in enumerate(tiles)
        ]
    raise ValueError(f"unknown area: {area!r} (expected 'fixture', 'zones' or 'astana')")


def build_query(filters: Sequence[str], timeout_s: int = int(TIMEOUT_S)) -> str:
    """POI query: every selector x every spatial filter, union, `out center tags`."""
    lines = [f"[out:json][timeout:{timeout_s}];", "("]
    for flt in filters:
        for sel in SELECTORS:
            lines.append(f"  nwr{sel}({flt});")
    lines += [");", "out center tags;"]
    return "\n".join(lines)


def user_agent() -> str:
    return f"QadamHackathon/1.0 ({get_settings().contact})"


def status_url(endpoint: str) -> str:
    return endpoint.rsplit("/", 1)[0] + "/status"


def mirror_state(client: httpx.Client, endpoint: str) -> str:
    """'ready' | 'busy' | 'down' | 'unknown' from the mirror's /api/status.

    Only an unreachable host is 'down'. Not every mirror proxies /status (mail.ru answers
    504 there even when its interpreter works), so any HTTP error is 'unknown' and the
    mirror is still tried.
    """
    try:
        resp = client.get(status_url(endpoint), timeout=STATUS_TIMEOUT_S)
    except httpx.HTTPError:
        return "down"
    if resp.status_code != 200:
        return "unknown"
    text = resp.text
    if "available now" in text:
        return "ready"
    if "Slot available after" in text or "Currently running queries" in text:
        return "busy"
    return "unknown"


def run_query(
    query: str,
    *,
    client: httpx.Client | None = None,
    endpoints: Sequence[str] = ENDPOINTS,
    pauses: Sequence[float] = RETRY_PAUSES_S,
    sleep: Callable[[float], None] = time.sleep,
    check_status: bool = False,
) -> dict:
    """POST the query to the mirrors in turn until one returns JSON."""
    own_client = client is None
    if client is None:
        client = httpx.Client(timeout=TIMEOUT_S, headers={"User-Agent": user_agent()})
    errors: list[str] = []
    try:
        for attempt in range(len(pauses) + 1):
            for url in endpoints:
                if check_status:
                    state = mirror_state(client, url)
                    if state in {"down", "busy"}:
                        errors.append(f"{url}: status {state}")
                        log.warning("overpass %s skipped: status %s", url, state)
                        continue
                try:
                    resp = client.post(url, data={"data": query})
                except httpx.HTTPError as exc:
                    errors.append(f"{url}: {type(exc).__name__}")
                    log.warning("overpass %s failed: %s", url, exc)
                    continue
                if resp.status_code == 200:
                    try:
                        payload = resp.json()
                    except ValueError:
                        errors.append(f"{url}: invalid JSON")
                        continue
                    payload.setdefault("qadam", {})["endpoint"] = url
                    return payload
                errors.append(f"{url}: HTTP {resp.status_code}")
                log.warning("overpass %s -> HTTP %s", url, resp.status_code)
            if attempt < len(pauses):
                sleep(pauses[attempt])
    finally:
        if own_client:
            client.close()
    raise OverpassError("Overpass unavailable: " + "; ".join(errors[-6:]))


def raw_dir_default() -> Path:
    return get_settings().data_dir / "raw"


def raw_path(name: str, raw_dir: Path | None = None) -> Path:
    return (raw_dir or raw_dir_default()) / f"osm_{name}.json"


def is_fresh(path: Path, ttl_s: float = CACHE_TTL_S, now: float | None = None) -> bool:
    if not path.exists():
        return False
    age = (now if now is not None else time.time()) - path.stat().st_mtime
    return age < ttl_s


def prefer(first: str | None, endpoints: Sequence[str]) -> list[str]:
    """Put the mirror that answered last at the front."""
    if first is None or first not in endpoints:
        return list(endpoints)
    return [first, *(e for e in endpoints if e != first)]


def save_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


@dataclass
class FetchResult:
    done: list[Path]
    cached: list[str]
    failed: list[str]


def fetch_area(
    area: str,
    *,
    force: bool = False,
    raw_dir: Path | None = None,
    query_fn: Callable[..., dict] = run_query,
    sleep: Callable[[float], None] = time.sleep,
    deadline: float | None = None,
) -> FetchResult:
    """Fetch every chunk of the area that has no fresh cache. A failed chunk is reported,
    not raised, so the rest still downloads; rerun to fill the gaps. Stops starting new
    chunks after `deadline` (unix time)."""
    result = FetchResult(done=[], cached=[], failed=[])
    last_good: str | None = None
    requested = False
    for chunk in area_chunks(area):
        path = raw_path(chunk.name, raw_dir)
        if not force and is_fresh(path):
            result.cached.append(chunk.name)
            result.done.append(path)
            continue
        if deadline is not None and time.time() > deadline:
            result.failed.append(f"{chunk.name}: deadline")
            continue
        if requested:
            sleep(BETWEEN_CHUNKS_S)
        requested = True
        started = time.time()
        try:
            payload = query_fn(
                build_query(chunk.filters), endpoints=prefer(last_good, ENDPOINTS),
                check_status=True,
            )
        except OverpassError as exc:
            log.error("chunk %s failed: %s", chunk.name, exc)
            result.failed.append(f"{chunk.name}: {exc}")
            continue
        last_good = payload.get("qadam", {}).get("endpoint")
        payload.setdefault("qadam", {}).update(
            {
                "area": area,
                "chunk": chunk.name,
                "filters": list(chunk.filters),
                "fetched_at_unix": int(started),
                "elapsed_s": round(time.time() - started, 1),
            }
        )
        save_json(path, payload)
        log.info("chunk %s: %d elements", chunk.name, len(payload.get("elements", [])))
        result.done.append(path)
    return result


def deadline_today(hhmm: str) -> float:
    hh, mm = (int(x) for x in hhmm.split(":"))
    now = time.localtime()
    return time.mktime((*now[:3], hh, mm, 0, *now[6:]))


def fetch_until(
    area: str,
    deadline: float,
    *,
    force: bool = False,
    raw_dir: Path | None = None,
    query_fn: Callable[..., dict] = run_query,
    sleep: Callable[[float], None] = time.sleep,
    round_pause_s: float = ROUND_PAUSE_S,
) -> FetchResult:
    """Repeat rounds (each fetches only missing chunks) until all are on disk or the
    deadline passes. Whatever is cached by then is what the pipeline works with."""
    while True:
        res = fetch_area(
            area, force=force, raw_dir=raw_dir, query_fn=query_fn, sleep=sleep, deadline=deadline
        )
        force = False  # a forced refresh applies to the first round only
        if not res.failed or time.time() + round_pause_s > deadline:
            return res
        log.warning("%d chunks missing, next round in %.0f s", len(res.failed), round_pause_s)
        sleep(round_pause_s)


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--area", choices=["fixture", "zones", "astana"], default="astana")
    parser.add_argument("--force", action="store_true", help="ignore the 24 h cache")
    parser.add_argument(
        "--deadline", help="local HH:MM: retry missing chunks in rounds until then, then stop"
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=get_settings().log_level, format="%(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    if args.deadline:
        res = fetch_until(args.area, deadline_today(args.deadline), force=args.force)
    else:
        res = fetch_area(args.area, force=args.force)
    print(f"ok {len(res.done)} (cached {len(res.cached)}), failed {len(res.failed)}", flush=True)
    for item in res.failed:
        print("FAILED", item[:200], flush=True)


if __name__ == "__main__":
    main()
