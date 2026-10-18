"""pipeline.run_all end to end on a temp data dir: no network, no API keys."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from app.config import get_settings
from app.schemas import POI

from .conftest import ROOT


@pytest.fixture
def tmp_data(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    data = tmp_path / "data"
    (data / "raw").mkdir(parents=True)
    shutil.copy(ROOT / "data" / "landmarks.json", data / "landmarks.json")
    payload = {
        "qadam": {"fetched_at_unix": 1792314269, "chunk": "zone_test", "endpoint": "fake"},
        "elements": [
            {"type": "node", "id": 1, "lat": 51.1283, "lon": 71.4305,
             "tags": {"amenity": "cafe", "name": "Кофе", "opening_hours": "Mo-Su 08:00-22:00"}},
            {"type": "way", "id": 2, "center": {"lat": 51.13, "lon": 71.43},
             "tags": {"leisure": "park", "name": "Парк"}},
        ],
    }
    (data / "raw" / "osm_zone_test.json").write_text(json.dumps(payload), encoding="utf-8")
    for name in ("QADAM_POI_PATH", "QADAM_INDEX_DIR", "QADAM_LANDMARKS_PATH", "QADAM_CACHE_DIR"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("QADAM_DATA_DIR", str(data))
    monkeypatch.setenv("GEMINI_API_KEY", "")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    get_settings.cache_clear()
    yield data
    get_settings.cache_clear()


def test_run_all_skip_fetch_builds_poi_and_report(tmp_data: Path) -> None:
    from pipeline.run_all import run

    notes = run("zones", skip_fetch=True, skip_enrich=True)
    rows = (tmp_data / "processed" / "poi.jsonl").read_text(encoding="utf-8").splitlines()
    pois = [POI.model_validate_json(r) for r in rows]
    assert [p.id for p in pois] == ["osm:node/1", "osm:way/2"]
    report = (tmp_data / "processed" / "REPORT.md").read_text(encoding="utf-8")
    assert "2 POI" in report and "не построен" in report
    assert any("GEMINI_API_KEY" in n for n in notes)
    assert not (tmp_data / "index" / "meta.json").exists()

    # idempotent: a second run gives the same file
    run("zones", skip_fetch=True, skip_enrich=True)
    assert (tmp_data / "processed" / "poi.jsonl").read_text(encoding="utf-8").splitlines() == rows
