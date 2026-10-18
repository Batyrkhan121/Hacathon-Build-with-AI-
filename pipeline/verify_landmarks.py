"""Check data/landmarks.json against the OSM objects they stand for.

    python -m pipeline.verify_landmarks            # print the drift table
    python -m pipeline.verify_landmarks --write    # take OSM centres, set verified=true

A landmark without an entry in LANDMARK_OSM keeps verified=false.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Mapping, Sequence
from pathlib import Path

from app.config import get_settings
from pipeline.fetch_osm import run_query
from pipeline.normalize import element_point, haversine_m

# landmark id -> OSM object whose centre is the anchor (looked up 2026-10-18)
LANDMARK_OSM: dict[str, str] = {
    "expo": "way/454857501",  # the Nur Alem sphere, tagged in OSM as "Alem.ai" (office)
    "mega_silk_way": "way/499328514",  # shop=mall, building
    "baiterek": "way/230401645",  # historic=monument, Q804210
    "khan_shatyr": "way/460703779",  # shop=mall, Q671484
    "palace_peace": "way/166197368",  # Palace of Peace and Reconciliation, Q2119741
    "hazrat_sultan": "way/240860325",  # place_of_worship, Q4494668
    "airport_nqz": "way/166500591",  # aeroway=terminal T1 (walk-relevant, not the runway)
    "astana_opera": "way/917478479",  # amenity=theatre, Q21371563
    "nurly_zhol": "way/1484667027",  # railway=station, Q137551678
}
COORD_DIGITS = 5


def lookup_query(refs: Sequence[str]) -> str:
    by_type: dict[str, list[str]] = {}
    for ref in refs:
        kind, osm_id = ref.split("/")
        by_type.setdefault(kind, []).append(osm_id)
    parts = [f"  {kind}(id:{','.join(ids)});" for kind, ids in sorted(by_type.items())]
    return "\n".join(["[out:json][timeout:60];", "(", *parts, ");", "out center tags;"])


def osm_centers(payload: Mapping) -> dict[str, tuple[float, float]]:
    out: dict[str, tuple[float, float]] = {}
    for el in payload.get("elements", []):
        point = element_point(el)
        if point:
            out[f"{el['type']}/{el['id']}"] = point
    return out


def apply(
    landmarks: list[dict], centers: Mapping[str, tuple[float, float]]
) -> list[tuple[str, float | None]]:
    """Move each mapped landmark to its OSM centre; returns (id, drift in metres or None)."""
    report: list[tuple[str, float | None]] = []
    for lm in landmarks:
        ref = LANDMARK_OSM.get(lm["id"])
        if ref is None or ref not in centers:
            report.append((lm["id"], None))
            continue
        lat, lon = centers[ref]
        drift = haversine_m(lm["geo"]["lat"], lm["geo"]["lon"], lat, lon)
        lm["geo"] = {"lat": round(lat, COORD_DIGITS), "lon": round(lon, COORD_DIGITS)}
        lm["verified"] = True
        report.append((lm["id"], drift))
    return report


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args(argv)
    path: Path = get_settings().landmarks_path
    landmarks = json.loads(path.read_text(encoding="utf-8"))
    centers = osm_centers(run_query(lookup_query(list(LANDMARK_OSM.values()))))
    for lm_id, drift in apply(landmarks, centers):
        status = "not mapped / not found" if drift is None else f"drift {drift:.0f} m"
        print(f"{lm_id:15s} {status}")
    if args.write:
        text = json.dumps(landmarks, ensure_ascii=False, indent=2) + "\n"
        path.write_text(text, encoding="utf-8")
        print(f"written {path}")


if __name__ == "__main__":
    main()
