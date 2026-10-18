"""pipeline.run_all end to end on a temp data dir: no network, no API keys."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.config import get_settings
from app.schemas import POI


@pytest.fixture
def tmp_data(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    data = tmp_path / "data"
    (data / "raw").mkdir(parents=True)
    # no landmarks: this test is about the step chain; landmark POIs are tested in normalize
    (data / "landmarks.json").write_text("[]", encoding="utf-8")
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


def test_enrich_gets_the_area(tmp_data: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import pipeline.enrich
    from pipeline.run_all import run

    calls: list[tuple] = []
    monkeypatch.setattr(pipeline.enrich, "run",
                        lambda limit=None, area="astana": calls.append((limit, area)) or [])
    run("zones", skip_fetch=True)
    assert calls == [(None, "zones")]


def test_missing_third_party_package_is_not_hidden(tmp_data: Path,
                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    import importlib

    import pipeline.run_all as run_all

    def no_anthropic(name: str):
        raise ModuleNotFoundError("No module named 'anthropic'", name="anthropic")

    monkeypatch.setattr(run_all.importlib, "import_module", no_anthropic)
    with pytest.raises(ModuleNotFoundError):
        run_all.run("zones", skip_fetch=True)

    def no_enrich(name: str):
        raise ModuleNotFoundError("No module named 'pipeline.enrich'", name="pipeline.enrich")

    monkeypatch.setattr(run_all.importlib, "import_module", no_enrich)
    notes = run_all.run("zones", skip_fetch=True)
    assert any("ещё не подключено" in n for n in notes)
    assert importlib is not None


def test_report_lists_normalization_drops(tmp_data: Path) -> None:
    from pipeline.run_all import run

    notes = run("zones", skip_fetch=True, skip_enrich=True)
    line = next(n for n in notes if n.startswith("Нормализация не взяла"))
    assert "без имени (кроме площадок) 0" in line and "склеено дублей 0" in line
    report = (tmp_data / "processed" / "REPORT.md").read_text(encoding="utf-8")
    assert "Нормализация не взяла" in report


def test_skip_index_flags_a_mismatching_index(tmp_data: Path) -> None:
    import json as _json

    from pipeline.run_all import run

    index = tmp_data / "index"
    index.mkdir()
    (index / "meta.json").write_text(_json.dumps({"status": "ok", "count": 99, "model": "m",
                                                  "dim": 4}), encoding="utf-8")
    notes = run("zones", skip_fetch=True, skip_enrich=True, skip_index=True)
    meta = _json.loads((index / "meta.json").read_text(encoding="utf-8"))
    assert meta["status"] == "stale" and "--skip-index" in meta["stale_reason"]
    assert "Индекс не пересобирался (--skip-index)." in notes


def test_skip_index_keeps_a_known_stale_reason(tmp_data: Path) -> None:
    import json as _json

    from pipeline.run_all import run

    index = tmp_data / "index"
    index.mkdir()
    (index / "meta.json").write_text(_json.dumps({
        "status": "stale", "stale_reason": "дневная квота Gemini исчерпана", "count": 99,
        "model": "m", "dim": 4}), encoding="utf-8")
    run("zones", skip_fetch=True, skip_enrich=True, skip_index=True)
    meta = _json.loads((index / "meta.json").read_text(encoding="utf-8"))
    assert meta["stale_reason"] == "дневная квота Gemini исчерпана"
