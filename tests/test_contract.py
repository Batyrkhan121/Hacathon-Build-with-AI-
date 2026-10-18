"""Contract tests: fixtures and data files must match app/schemas.py. Owner: INTEGRATOR."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.schemas import POI, TAG_VOCAB, Answer, LocationInput, RecommendRequest

from .conftest import FIXTURES, ROOT, read_jsonl


def test_synthetic_pois_valid(synthetic_pois: list[POI]) -> None:
    ids = [p.id for p in synthetic_pois]
    assert len(ids) == len(set(ids))
    for poi in synthetic_pois:
        assert set(poi.tags) <= TAG_VOCAB, poi.id


def test_landmarks_valid(landmarks) -> None:
    assert {"expo", "mega_silk_way", "airport_nqz"} <= set(landmarks)
    for lm in landmarks.values():
        assert all(alias == alias.lower() for alias in lm.aliases), lm.id


def test_mock_answer_valid() -> None:
    raw = json.loads((ROOT / "web" / "mock" / "answer_sample.json").read_text(encoding="utf-8"))
    answer = Answer.model_validate(raw)
    assert 1 <= len(answer.cards) <= 3
    assert [c.rank for c in answer.cards] == list(range(1, len(answer.cards) + 1))


@pytest.mark.parametrize(
    "path",
    [FIXTURES / "poi_sample.jsonl", ROOT / "data" / "processed" / "poi.jsonl"],
    ids=["poi_sample", "poi_full"],
)
def test_real_data_valid_if_present(path: Path) -> None:
    if not path.exists():
        pytest.skip(f"{path.name} not produced yet (DATA lane)")
    rows = read_jsonl(path)[:2000]
    assert rows, "file is empty"
    pois = [POI.model_validate(r) for r in rows]
    assert len({p.id for p in pois}) == len(pois)
    bad_tags = {t for p in pois for t in p.tags} - TAG_VOCAB
    assert not bad_tags, f"tags outside TAG_VOCAB: {sorted(bad_tags)}"


def test_request_rejects_raw_coordinates() -> None:
    with pytest.raises(ValidationError):
        RecommendRequest.model_validate(
            {"query": "кофе", "location": {"lat": 51.1, "lon": 71.4}}
        )


def test_location_needs_exactly_one_field() -> None:
    with pytest.raises(ValidationError):
        LocationInput()
    with pytest.raises(ValidationError):
        LocationInput(h3_cell="892153833a7ffff", landmark_id="expo")
    assert LocationInput(landmark_id="expo").landmark_id == "expo"


def test_default_embeddings_settings_match_the_index(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without EMBED_* variables the query model must be the one data/index was built with,
    otherwise Store drops the index and search silently falls back to words (Docker smoke 18.10)."""
    from app.config import get_settings

    meta_path = ROOT / "data" / "index" / "meta.json"
    if not meta_path.exists():
        pytest.skip("no data/index/meta.json")
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    for name in ("EMBED_PROVIDER", "EMBED_MODEL", "EMBED_DIM"):
        monkeypatch.delenv(name, raising=False)
    get_settings.cache_clear()
    s = get_settings()
    assert s.embed_provider == meta.get("provider", "gemini") == "openrouter"
    assert (s.embed_model, s.embed_dim) == (meta["model"], meta["dim"])


def _route(**over: object) -> dict:
    base = {"stop_ids": ["osm:node/1"], "legs": [{"from_id": "origin", "to_id": "osm:node/1",
            "distance_m": 400, "walk_min": 7}], "total_walk_m": 400, "total_min": 27,
            "fits_budget": True,
            "path": [{"lat": 51.09, "lon": 71.41}, {"lat": 51.091, "lon": 71.412}],
            "note": "Маршрут приблизительный: прямые отрезки × 1,3"}
    return {**base, **over}


def test_mini_route_1_0_answers_stay_valid_with_straight_defaults() -> None:
    from app.schemas import MiniRoute

    r = MiniRoute.model_validate(_route())
    assert (r.path_source, r.crossings, r.steps_count) == ("straight", [], 0)


def test_mini_route_graph_fields() -> None:
    from app.schemas import MiniRoute

    crossing = {"lat": 51.0905, "lon": 71.411, "kind": "signals"}
    r = MiniRoute.model_validate(_route(path_source="graph", crossings=[crossing], steps_count=2))
    assert r.path_source == "graph" and r.crossings[0].kind == "signals" and r.steps_count == 2
    for bad in ({"path_source": "osrm"}, {"crossings": [crossing] * 51}, {"steps_count": -1},
                {"crossings": [{**crossing, "kind": "bridge"}]},
                {"crossings": [{**crossing, "h3_cell": "89215380487ffff"}]}):
        with pytest.raises(ValidationError):
            MiniRoute.model_validate(_route(**bad))


def test_walk_graph_flag_defaults(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """WALK_GRAPH defaults to on and points at data/processed/walk_graph.npz; it is ready only
    when the file exists (otherwise routes stay straight)."""
    from app.config import get_settings

    monkeypatch.delenv("WALK_GRAPH", raising=False)
    monkeypatch.delenv("QADAM_WALK_GRAPH_PATH", raising=False)
    get_settings.cache_clear()
    s = get_settings()
    assert s.walk_graph_enabled and s.walk_graph_path == s.data_dir / "processed" / "walk_graph.npz"
    assert s.walk_graph_meta_path == s.data_dir / "processed" / "walk_graph_meta.json"
    graph = tmp_path / "walk_graph.npz"
    monkeypatch.setenv("QADAM_WALK_GRAPH_PATH", str(graph))
    get_settings.cache_clear()
    assert not get_settings().walk_graph_ready  # no file yet
    graph.write_bytes(b"")
    get_settings.cache_clear()
    assert get_settings().walk_graph_ready
    monkeypatch.setenv("WALK_GRAPH", "false")
    get_settings.cache_clear()
    assert not get_settings().walk_graph_ready
