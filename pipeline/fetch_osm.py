"""OSM Overpass -> data/raw/osm_<area>.json.

Mirrors are tried in turn on network errors, 429 and 5xx; up to 3 rounds with 5 -> 15 -> 45 s
pauses. A raw dump younger than 24 h is reused without touching the network.

    python -m pipeline.fetch_osm --area fixture
    python -m pipeline.fetch_osm --area astana --force
"""

from __future__ import annotations

import argparse
import json
import logging
import time
from collections.abc import Callable, Sequence
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
TIMEOUT_S = 180.0
CACHE_TTL_S = 24 * 3600

# south, west, north, east
ASTANA_BBOX: tuple[float, float, float, float] = (51.03, 71.20, 51.27, 71.65)
# T10 fixture: real POIs within 1.5 km of EXPO (Nur Alem) and Mega Silk Way
FIXTURE_CENTERS: tuple[tuple[float, float], ...] = ((51.0894, 71.4161), (51.0889, 71.4103))
FIXTURE_RADIUS_M = 1500

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


def area_filters(area: str) -> list[str]:
    """Overpass spatial filters (without parentheses) for a named area."""
    if area == "astana":
        s, w, n, e = ASTANA_BBOX
        return [f"{s},{w},{n},{e}"]
    if area == "fixture":
        return [f"around:{FIXTURE_RADIUS_M},{lat},{lon}" for lat, lon in FIXTURE_CENTERS]
    raise ValueError(f"unknown area: {area!r} (expected 'astana' or 'fixture')")


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


def run_query(
    query: str,
    *,
    client: httpx.Client | None = None,
    endpoints: Sequence[str] = ENDPOINTS,
    pauses: Sequence[float] = RETRY_PAUSES_S,
    sleep: Callable[[float], None] = time.sleep,
) -> dict:
    """POST the query to the mirrors in turn until one returns JSON."""
    own_client = client is None
    if client is None:
        client = httpx.Client(timeout=TIMEOUT_S, headers={"User-Agent": user_agent()})
    errors: list[str] = []
    try:
        for attempt in range(len(pauses) + 1):
            for url in endpoints:
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


def raw_path(area: str, raw_dir: Path | None = None) -> Path:
    base = raw_dir or get_settings().data_dir / "raw"
    return base / f"osm_{area}.json"


def is_fresh(path: Path, ttl_s: float = CACHE_TTL_S, now: float | None = None) -> bool:
    if not path.exists():
        return False
    age = (now if now is not None else time.time()) - path.stat().st_mtime
    return age < ttl_s


def fetch_area(area: str, *, force: bool = False, raw_dir: Path | None = None) -> Path:
    """Download the area into data/raw/osm_<area>.json unless a fresh copy exists."""
    path = raw_path(area, raw_dir)
    if not force and is_fresh(path):
        log.info("using cached %s", path)
        return path
    query = build_query(area_filters(area))
    started = time.time()
    payload = run_query(query)
    elapsed_s = round(time.time() - started, 1)
    payload["qadam"].update(
        {"area": area, "fetched_at_unix": int(started), "elapsed_s": elapsed_s}
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)
    log.info("saved %d elements to %s", len(payload.get("elements", [])), path)
    return path


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--area", choices=["astana", "fixture"], default="astana")
    parser.add_argument("--force", action="store_true", help="ignore the 24 h cache")
    args = parser.parse_args(argv)
    logging.basicConfig(level=get_settings().log_level, format="%(levelname)s %(message)s")
    print(fetch_area(args.area, force=args.force))


if __name__ == "__main__":
    main()
