"""Checks on the committed dataset (data/processed/poi.jsonl): coverage the demo relies on."""

from __future__ import annotations

import json

import pytest

from app.schemas import POI
from pipeline.normalize import haversine_m

from .conftest import ROOT, read_jsonl

POI_PATH = ROOT / "data" / "processed" / "poi.jsonl"
# 20 min walk at 75 m/min with the 1.3 detour factor used by app.geo
TWENTY_MIN_STRAIGHT_M = 20 * 75 / 1.3


@pytest.fixture(scope="module")
def pois() -> list[POI]:
    if not POI_PATH.exists():
        pytest.skip("poi.jsonl not built")
    return [POI.model_validate(r) for r in read_jsonl(POI_PATH)]


@pytest.fixture(scope="module")
def landmarks() -> dict[str, dict]:
    raw = json.loads((ROOT / "data" / "landmarks.json").read_text(encoding="utf-8"))
    return {lm["id"]: lm for lm in raw}


@pytest.mark.parametrize("origin", ["expo", "mega_silk_way"])
def test_expo_sphere_is_a_museum_within_20_min(pois, landmarks, origin: str) -> None:
    g = landmarks[origin]["geo"]
    sphere = next(p for p in pois if p.id == "osm:way/454857501")
    # data/manual/overrides.json: the sphere is a museum; hours only with a source
    assert sphere.category == "museum" and "manual" in sphere.sources
    assert sphere.opening_hours and sphere.website  # hours only with a source link
    assert haversine_m(g["lat"], g["lon"], sphere.geo.lat, sphere.geo.lon) < TWENTY_MIN_STRAIGHT_M


def test_no_duplicate_landmark_pois(pois) -> None:
    for osm_id in ("osm:way/230401645", "osm:way/460703779", "osm:way/240860325",
                   "osm:way/917478479"):  # Baiterek, Khan Shatyr, Hazrat Sultan, Opera
        same = [p for p in pois if p.id == osm_id]
        assert len(same) <= 1
        if same:
            assert "manual" not in same[0].sources  # came through its own OSM tags


def test_alem_ai_record_from_official_site(pois) -> None:
    sphere = next(p for p in pois if p.id == "osm:way/454857501")
    assert sphere.opening_hours == "Tu-Su 10:00-18:30; Mo off"
    assert sphere.website and sphere.website.startswith("https://alem.ai")
    assert sphere.description_source == "manual" and "wikidata" in sphere.sources
    dump = sphere.model_dump_json().casefold()
    assert "energy" not in dump and "энерги" not in dump  # the old museum is gone
