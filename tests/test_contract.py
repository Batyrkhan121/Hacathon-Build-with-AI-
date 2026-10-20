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


@pytest.mark.parametrize("name", ["answer_sample.json", "answer_route.json"])
def test_mock_answer_valid(name: str) -> None:
    raw = json.loads((ROOT / "web" / "mock" / name).read_text(encoding="utf-8"))
    answer = Answer.model_validate(raw)
    assert 1 <= len(answer.cards) <= 3
    assert [c.rank for c in answer.cards] == list(range(1, len(answer.cards) + 1))
    if name == "answer_route.json":  # the walk-graph mock of web/?mock=route
        assert answer.route is not None and answer.route.path_source == "graph"
        assert answer.route.crossings and len(answer.route.path) >= 2


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
    unknown = MiniRoute.model_validate(_route(crossings=[{**crossing, "kind": "unknown"}]))
    assert unknown.crossings[0].kind == "unknown"  # 1.3.0: OSM gives no crossing type
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


def test_location_accepts_cells_up_to_r10() -> None:
    import h3

    from app.schemas import MAX_H3_RES

    assert MAX_H3_RES == 10
    for res in (8, 9, 10):
        LocationInput(h3_cell=h3.latlng_to_cell(51.0894, 71.4161, res))
    for res in (11, 13, 15):
        with pytest.raises(ValidationError, match="finer than r10"):
            LocationInput(h3_cell=h3.latlng_to_cell(51.0894, 71.4161, res))


# ------------------------------------------------------------------ 1.3.0 (wave 8)


def _event_card(**over: object) -> dict:
    base = {"rank": 1, "poi_id": "event:jazz_evening", "title": "Джазовый вечер",
            "category": "entertainment", "geo": {"lat": 51.09, "lon": 71.41}, "distance_m": 400,
            "walk_min": 6, "visit_min_low": 60, "visit_min_high": 120, "why": "Живая музыка.",
            "what_to_do": "Приходите к началу.", "open_status": "unknown",
            "open_confidence": "low", "open_note": "нет данных о часах, проверьте перед визитом",
            "idea_source": "poi_data", "facts_sources": ["manual"], "data_confidence": 0.8,
            "address": "Филармония, пр. Мангилик Ел, 1", "kind": "event",
            "starts_at": "2026-10-24T19:00:00+05:00", "ends_at": "2026-10-24T21:00:00+05:00",
            "source_url": "https://example.org/events/jazz"}
    return {**base, **over}


def test_1_2_0_request_gets_1_3_0_defaults() -> None:
    from app.schemas import SCHEMA_VERSION

    assert SCHEMA_VERSION == "1.3.0"
    req = RecommendRequest.model_validate({"query": "кофе", "location": {"landmark_id": "expo"}})
    assert (req.route_profile, req.prefs) == ("fastest", None)
    for profile in ("fastest", "accessible", "lit"):
        assert RecommendRequest.model_validate(
            {"query": "кофе", "location": {"landmark_id": "expo"}, "route_profile": profile}
        ).route_profile == profile
    with pytest.raises(ValidationError):
        RecommendRequest.model_validate(
            {"query": "кофе", "location": {"landmark_id": "expo"}, "route_profile": "safe"})


def test_prefs_take_only_the_fixed_vocabulary() -> None:
    from app.schemas import MAX_PREFS, PREF_LABELS_RU, UserPrefs

    assert len(PREF_LABELS_RU) == MAX_PREFS == 8
    assert set(PREF_LABELS_RU.values()) == {"тихо", "кофе", "с ребёнком", "музеи", "парки",
                                            "без баров", "в тепле", "ночные"}
    every = [t for t in PREF_LABELS_RU if t != "no_bars"]
    ok = UserPrefs(likes=["no_bars", "museums"], avoids=["night"])
    assert (ok.likes, ok.avoids) == (["no_bars", "museums"], ["night"])
    assert UserPrefs(likes=list(PREF_LABELS_RU)).likes == list(PREF_LABELS_RU)
    assert UserPrefs(avoids=every).avoids == every
    for bad in ({"likes": ["sushi"]}, {"likes": ["музеи"]}, {"avoids": ["coffee please"]},
                {"likes": ["museums", "museums"]}, {"likes": ["parks"], "avoids": ["parks"]},
                {"avoids": ["no_bars"]}, {"likes": list(PREF_LABELS_RU) * 2},
                {"likes": [], "history": ["osm:node/1"]}, {"text": "люблю тишину"},
                {"likes": "quiet"}):
        with pytest.raises(ValidationError):
            UserPrefs.model_validate(bad)


def test_prefs_stay_out_of_request_repr() -> None:
    req = RecommendRequest.model_validate(
        {"query": "кофе", "location": {"landmark_id": "expo"},
         "prefs": {"likes": ["museums"], "avoids": ["night"]}})
    assert req.prefs is not None and req.prefs.likes == ["museums"]
    assert "museums" not in repr(req) and "prefs" not in repr(req) and "museums" not in str(req)


def test_unknown_pref_tag_is_a_private_422(synthetic_pois: list[POI], landmarks,
                                           tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    from app import api
    from app.pipeline import Deps

    from .test_pipeline import FakeLLM, make_settings, make_store

    deps = Deps(store=make_store(synthetic_pois, landmarks), llm=FakeLLM(),
                settings=make_settings(tmp_path))
    client = TestClient(api.create_app(deps))
    base = {"query": "кофе", "location": {"landmark_id": "expo"}}
    for prefs in ({"likes": ["secret_tag_x"]}, {"avoids": ["no_bars"]}, {"likes": "museums"}):
        r = client.post("/api/recommend", json={**base, "prefs": prefs})
        assert r.status_code == 422, prefs
        assert "secret_tag_x" not in r.text
    r = client.post("/api/recommend", json={**base, "route_profile": "night_vision"})
    assert r.status_code == 422


def test_mini_route_1_3_0_profile_facts() -> None:
    from app.schemas import MiniRoute

    r = MiniRoute.model_validate(_route())
    assert r.profile == "fastest"
    assert (r.lit_share, r.unlit_share, r.unknown_lit_share) == (None, None, None)
    assert (r.kerb_lowered, r.kerb_unknown, r.max_outdoor_leg_min) == (None, None, None)
    full = MiniRoute.model_validate(_route(
        path_source="graph", profile="lit", lit_share=0.78, unlit_share=0.07,
        unknown_lit_share=0.15, steps_count=0, kerb_lowered=3, kerb_unknown=2,
        max_outdoor_leg_min=9))
    assert (full.profile, full.lit_share, full.kerb_lowered, full.max_outdoor_leg_min) == (
        "lit", 0.78, 3, 9)
    straight = MiniRoute.model_validate(_route(profile="accessible", lit_share=0,
                                               unlit_share=0, unknown_lit_share=1))
    assert straight.unknown_lit_share == 1
    for bad in ({"profile": "safe"}, {"lit_share": 0.5}, {"lit_share": 0.5, "unlit_share": 0.5},
                {"lit_share": 0.6, "unlit_share": 0.3, "unknown_lit_share": 0.3},
                {"lit_share": 1.2, "unlit_share": 0, "unknown_lit_share": -0.2},
                {"kerb_lowered": -1}, {"kerb_unknown": -1}, {"max_outdoor_leg_min": -5}):
        with pytest.raises(ValidationError):
            MiniRoute.model_validate(_route(**bad))


def test_place_cards_stay_valid_and_event_cards_are_checked() -> None:
    from app.schemas import Card

    raw = json.loads((ROOT / "web" / "mock" / "answer_sample.json").read_text(encoding="utf-8"))
    place = Card.model_validate(raw["cards"][0])
    assert (place.kind, place.starts_at, place.ends_at, place.source_url) == (
        "place", None, None, None)
    event = Card.model_validate(_event_card())
    assert event.kind == "event" and event.starts_at is not None
    assert event.starts_at.utcoffset() is not None
    assert Card.model_validate(_event_card(ends_at=None)).ends_at is None
    for bad in ({"starts_at": None}, {"source_url": None},
                {"starts_at": "2026-10-24T19:00:00"},
                {"ends_at": "2026-10-24T18:00:00+05:00"},
                {"ends_at": "2026-10-24T19:00:00+05:00"},
                {"source_url": "http://example.org/events/jazz"},
                {"source_url": "javascript:alert(1)"},
                {"source_url": "https://example.org/a b"},
                {"kind": "place"}, {"kind": "concert"}):
        with pytest.raises(ValidationError):
            Card.model_validate(_event_card(**bad))
    for leak in ({"starts_at": "2026-10-24T19:00:00+05:00"},
                 {"source_url": "https://example.org/x"}):
        with pytest.raises(ValidationError):
            Card.model_validate({**raw["cards"][0], **leak})
