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
from pipeline.landmark_refs import LANDMARK_OSM
from pipeline.normalize import element_point, haversine_m

COORD_DIGITS = 5
# beyond this the OSM object is probably the wrong one (or was moved): do not apply it
# blindly; the airport moved 685 m to its terminal on purpose and stays under the limit
MAX_DRIFT_M = 1000.0


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
    """Move each mapped landmark to its OSM centre; returns (id, drift in metres or None).
    A drift above MAX_DRIFT_M is reported but not applied, and `verified` is not set."""
    report: list[tuple[str, float | None]] = []
    for lm in landmarks:
        ref = LANDMARK_OSM.get(lm["id"])
        if ref is None or ref not in centers:
            report.append((lm["id"], None))
            continue
        lat, lon = centers[ref]
        drift = haversine_m(lm["geo"]["lat"], lm["geo"]["lon"], lat, lon)
        if drift > MAX_DRIFT_M:
            report.append((lm["id"], drift))
            continue
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
        if drift is None:
            status = "not mapped / not found"
        elif drift > MAX_DRIFT_M:
            status = f"drift {drift:.0f} m > {MAX_DRIFT_M:.0f} m: NOT applied, check LANDMARK_OSM"
        else:
            status = f"drift {drift:.0f} m"
        print(f"{lm_id:15s} {status}")
    if args.write:
        text = json.dumps(landmarks, ensure_ascii=False, indent=2) + "\n"
        path.write_text(text, encoding="utf-8")
        print(f"written {path}")


if __name__ == "__main__":
    main()
