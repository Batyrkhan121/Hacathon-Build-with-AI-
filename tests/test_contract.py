"""Contract tests: fixtures and data files must match app/schemas.py. Owner: INTEGRATOR."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.schemas import (
    GAP_H3_RES,
    GAP_MIN_CROSSINGS,
    GAP_MIN_POI,
    GAP_MIN_WALK_M,
    ODBL_URL,
    OSM_ATTRIBUTION,
    OSM_COPYRIGHT_URL,
    POI,
    TAG_VOCAB,
    Answer,
    CityGaps,
    GapCounts,
    LocationInput,
    RecommendRequest,
)

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


@pytest.mark.parametrize("name", ["answer_sample.json", "answer_route.json", "answer_w8.json"])
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

    assert SCHEMA_VERSION == "1.4.1"  # 1.4.x adds the city gaps map only
    req = RecommendRequest.model_validate({"query": "кофе", "location": {"landmark_id": "expo"}})
    assert (req.route_profile, req.prefs) == ("fastest", None)
    for profile in ("fastest", "accessible", "lit"):
        assert RecommendRequest.model_validate(
            {"query": "кофе", "location": {"landmark_id": "expo"}, "route_profile": profile}
        ).route_profile == profile
    with pytest.raises(ValidationError):
        RecommendRequest.model_validate(
            {"query": "кофе", "location": {"landmark_id": "expo"}, "route_profile": "safe"})


def test_prefs_take_only_the_fixed_vocabularies() -> None:
    """1.3.1: likes and avoids have separate vocabularies; contradicting pairs are a 422."""
    from app.schemas import (
        MAX_PREFS,
        PREF_AVOID_LABELS_RU,
        PREF_CONFLICTS,
        PREF_LABELS_RU,
        PREF_LIKE_LABELS_RU,
        UserPrefs,
    )

    assert set(PREF_LIKE_LABELS_RU.values()) == {"тихо", "кофе", "с ребёнком", "музеи", "парки",
                                                 "в тепле", "ночные"}
    assert set(PREF_AVOID_LABELS_RU.values()) == {"бары", "шумно", "долго на улице",
                                                  "детские места", "кафе"}
    assert PREF_LABELS_RU == {**PREF_LIKE_LABELS_RU, **PREF_AVOID_LABELS_RU} and MAX_PREFS == 8
    assert PREF_CONFLICTS == {("kids", "kids_places"), ("coffee", "cafes")}
    likes, avoids = list(PREF_LIKE_LABELS_RU), list(PREF_AVOID_LABELS_RU)
    ok = UserPrefs(likes=["museums", "quiet"], avoids=["bars", "noisy"])
    assert (ok.likes, ok.avoids) == (["museums", "quiet"], ["bars", "noisy"])
    assert UserPrefs(likes=likes).likes == likes and UserPrefs(avoids=avoids).avoids == avoids
    assert UserPrefs(likes=["kids"], avoids=["bars"]).likes == ["kids"]
    for bad in ({"likes": ["sushi"]}, {"likes": ["музеи"]}, {"avoids": ["coffee please"]},
                {"avoids": ["no_bars"]},  # the 1.3.0 tag is only a synonym in likes
                {"likes": ["bars"]}, {"avoids": ["museums"]},  # a tag of the other list
                {"likes": ["quiet"], "avoids": ["quiet"]},  # one tag in both lists
                {"likes": ["kids"], "avoids": ["kids_places"]},  # contradicting pairs
                {"likes": ["coffee", "parks"], "avoids": ["cafes"]},
                {"likes": ["museums", "museums"]}, {"avoids": ["bars", "bars"]},
                {"likes": [], "history": ["osm:node/1"]}, {"text": "люблю тишину"},
                {"likes": "quiet"}):
        with pytest.raises(ValidationError):
            UserPrefs.model_validate(bad)


def test_prefs_stay_out_of_request_repr() -> None:
    req = RecommendRequest.model_validate(
        {"query": "кофе", "location": {"landmark_id": "expo"},
         "prefs": {"likes": ["museums"], "avoids": ["noisy"]}})
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
    for prefs in ({"likes": ["secret_tag_x"]}, {"avoids": ["no_bars"]}, {"likes": "museums"},
                  {"likes": ["kids"], "avoids": ["kids_places"]}):
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


def test_exclude_poi_ids_for_more() -> None:
    from app.schemas import MAX_EXCLUDE

    base = {"query": "кофе", "location": {"landmark_id": "expo"}}
    assert RecommendRequest.model_validate(base).exclude_poi_ids == []
    ids = [f"osm:node/{n}" for n in range(MAX_EXCLUDE - 1)] + ["event:jazz_evening"]
    req = RecommendRequest.model_validate({**base, "exclude_poi_ids": ids})
    assert req.exclude_poi_ids == ids and MAX_EXCLUDE == 9
    assert "osm:node/1" not in repr(req) and "exclude_poi_ids" not in repr(req)
    for bad in (ids + ["osm:way/77"], ["osm:node/1", "osm:node/1"], ["Музей Сингулярности"],
                ["osm:node/1 2"], ["javascript:alert(1)"], "osm:node/1", [""]):
        with pytest.raises(ValidationError):
            RecommendRequest.model_validate({**base, "exclude_poi_ids": bad})


def test_mini_route_1_3_1_straight_share() -> None:
    from app.schemas import MiniRoute

    assert MiniRoute.model_validate(_route()).straight_share is None
    for share in (0, 0.25, 1):
        assert MiniRoute.model_validate(_route(straight_share=share)).straight_share == share
    for bad in (-0.1, 1.01, "half"):
        with pytest.raises(ValidationError):
            MiniRoute.model_validate(_route(straight_share=bad))


def test_event_cards_carry_a_venue() -> None:
    from app.schemas import Card

    raw = json.loads((ROOT / "web" / "mock" / "answer_sample.json").read_text(encoding="utf-8"))
    assert Card.model_validate(raw["cards"][0]).venue is None
    assert Card.model_validate(_event_card(venue="Филармония")).venue == "Филармония"
    assert Card.model_validate(_event_card()).venue is None  # optional for an event
    with pytest.raises(ValidationError):
        Card.model_validate({**raw["cards"][0], "venue": "Филармония"})  # a place has none
    with pytest.raises(ValidationError):
        Card.model_validate(_event_card(venue="x" * 201))


def test_the_1_3_0_like_no_bars_is_read_as_the_avoid_bars() -> None:
    """Synonym for one version (Megah 21.10): an old saved «без баров» never gives a 422."""
    from app.schemas import DEPRECATED_LIKES, UserPrefs

    assert DEPRECATED_LIKES == {"no_bars": "bars"}
    p = UserPrefs.model_validate({"likes": ["no_bars", "museums"]})
    assert (p.likes, p.avoids) == (["museums"], ["bars"])
    p = UserPrefs.model_validate({"likes": ["no_bars"], "avoids": ["bars", "noisy"]})
    assert (p.likes, p.avoids) == ([], ["bars", "noisy"])
    p = UserPrefs.model_validate({"likes": ["no_bars", "no_bars"]})
    assert (p.likes, p.avoids) == ([], ["bars"])
    req = RecommendRequest.model_validate({"query": "кофе", "location": {"landmark_id": "expo"},
                                           "prefs": {"likes": ["no_bars"]}})
    assert req.prefs is not None and req.prefs.avoids == ["bars"]
    for bad in ({"likes": ["no_bars"], "avoids": "bars"}, {"likes": ["no_bars", "sushi"]},
                {"likes": ["no_bars", "kids"], "avoids": ["kids_places"]}):
        with pytest.raises(ValidationError):
            UserPrefs.model_validate(bad)


def test_events_path_defaults_to_data_and_can_point_elsewhere(monkeypatch: pytest.MonkeyPatch,
                                                              tmp_path: Path) -> None:
    """QADAM_EVENTS_PATH lets eval feed its own test events without touching data/events.json."""
    from app.config import get_settings

    monkeypatch.delenv("QADAM_EVENTS_PATH", raising=False)
    get_settings.cache_clear()
    s = get_settings()
    assert s.events_path == s.data_dir / "events.json"
    fixture = tmp_path / "events_eval.json"
    monkeypatch.setenv("QADAM_EVENTS_PATH", str(fixture))
    get_settings.cache_clear()
    assert get_settings().events_path == fixture
    get_settings.cache_clear()


# ---------------------------------------------------------------- 1.4.0: city gaps map

GAPS_MOCK = ROOT / "web" / "mock" / "city_gaps.json"
GAPS_DATA = ROOT / "data" / "processed" / "city_gaps.json"


def _gaps() -> dict:
    return json.loads(GAPS_MOCK.read_text(encoding="utf-8"))


def test_city_gaps_mock_valid() -> None:
    gaps = CityGaps.model_validate(_gaps())
    assert gaps.h3_res == GAP_H3_RES == 8
    assert gaps.attribution == OSM_ATTRIBUTION and gaps.license_url == ODBL_URL
    assert gaps.copyright_url == OSM_COPYRIGHT_URL
    # the mock shows both sides of the thresholds: a share and «мало данных»
    assert any(c.poi >= GAP_MIN_POI for c in gaps.cells)
    assert any(0 < c.poi < GAP_MIN_POI for c in gaps.cells)
    assert any(c.walk_m >= GAP_MIN_WALK_M for c in gaps.cells)
    assert any(c.crossings >= GAP_MIN_CROSSINGS for c in gaps.cells)


def test_city_gaps_data_file_valid() -> None:
    # built by DATA (pipeline/city_gaps.py, T141) and committed; the image serves it as is
    gaps = CityGaps.model_validate_json(GAPS_DATA.read_text(encoding="utf-8"))
    assert gaps.cells and gaps.city.poi > 0 and gaps.city.walk_m > 0


def _city_sum(doc: dict) -> None:
    for name in GapCounts.model_fields:
        doc["city"][name] = sum(cell[name] for cell in doc["cells"])


@pytest.mark.parametrize("break_it", [
    lambda d: d["cells"][0].update(poi_no_hours=d["cells"][0]["poi"] + 1),  # part > total
    lambda d: d["cells"][0].update(walk_lit_unknown_m=d["cells"][0]["walk_m"] + 1),
    lambda d: d["cells"][0].update(crossings_kerb_unknown=d["cells"][0]["crossings"] + 1),
    lambda d: d["cells"][0].update(h3_cell="891f1d48177ffff"),  # r9, not r8
    lambda d: d["cells"][0].update(h3_cell="zz1f1d4817fffff"),  # not an H3 index
    lambda d: d["cells"].append(dict(d["cells"][0])),  # duplicate cell
    lambda d: d["cells"].reverse(),  # not sorted
    lambda d: d["cells"][0].update(poi=0, poi_no_hours=0, poi_wheelchair_unknown=0,
                                   poi_no_name_kk=0, walk_m=0, walk_lit_unknown_m=0,
                                   crossings=0, crossings_kind_unknown=0,
                                   crossings_kerb_unknown=0),  # empty cell
    lambda d: d["cells"][0].update(poi_ids=["osm:node/1"]),  # no per-place lists
    lambda d: d.update(h3_cell="881f1d4817fffff"),  # no request echo / extra fields
    lambda d: d.update(attribution="Qadam"),
    lambda d: d.update(license_url="https://example.org/"),
    lambda d: d.update(built_at="2026-10-22T10:00:00"),  # naive time
    lambda d: d.update(poi_date="22.10.2026"),
    lambda d: d.update(h3_res=9),
])
def test_city_gaps_rejects(break_it) -> None:
    doc = _gaps()
    break_it(doc)
    _city_sum(doc)
    with pytest.raises(ValidationError):
        CityGaps.model_validate(doc)


def test_city_gaps_city_must_be_the_sum_over_cells() -> None:
    doc = _gaps()
    doc["city"]["poi"] += 1
    doc["city"]["poi_no_hours"] += 1
    with pytest.raises(ValidationError, match="sum over cells"):
        CityGaps.model_validate(doc)


def test_city_gaps_in_docker_allowlist() -> None:
    allow = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
    assert "!data/processed/city_gaps.json" in allow
    assert "!web/mock/city_gaps.json" in allow


def test_city_gaps_path_defaults_to_processed_and_can_point_elsewhere(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from app.config import get_settings

    monkeypatch.delenv("QADAM_CITY_GAPS_PATH", raising=False)
    get_settings.cache_clear()
    s = get_settings()
    assert s.city_gaps_path == s.data_dir / "processed" / "city_gaps.json"
    fixture = tmp_path / "city_gaps.json"
    monkeypatch.setenv("QADAM_CITY_GAPS_PATH", str(fixture))
    get_settings.cache_clear()
    assert get_settings().city_gaps_path == fixture
    get_settings.cache_clear()


# ------------------------------------------------------------ 1.4.1: districts (Megah, 22.10)


def _district(doc: dict, district_id: str) -> dict:
    return next(d for d in doc["districts"]["items"] if d["id"] == district_id)


def test_city_gaps_mock_districts_add_up_to_the_city() -> None:
    gaps = CityGaps.model_validate(_gaps())
    districts = gaps.districts
    assert districts is not None
    assert {d.status for d in districts.items} == {"ok", "stale", "missing"}
    total = districts.unassigned.model_dump()
    for d in districts.items:
        for name, value in (d.counts.model_dump() if d.counts else {}).items():
            total[name] += value
    assert total == gaps.city.model_dump()
    cells = sum(d.cell_count for d in districts.items) + districts.unassigned_cells
    assert cells == len(gaps.cells)


def test_city_gaps_without_districts_stays_valid() -> None:
    doc = _gaps()  # a 1.4.0 file: no districts block, no cell districts
    doc.pop("districts")
    for cell in doc["cells"]:
        cell.pop("district")
    assert CityGaps.model_validate(doc).districts is None


@pytest.mark.parametrize("break_it", [
    lambda d: d["cells"][0].update(district="nowhere"),
    lambda d: d["cells"][0].update(district="mock_c"),  # a district without an outline
    lambda d: _district(d, "mock_a")["counts"].update(poi=10_000, poi_no_hours=0),
    lambda d: _district(d, "mock_a").update(cell_count=_district(d, "mock_a")["cell_count"] + 1),
    lambda d: d["districts"]["unassigned"].update(poi=d["districts"]["unassigned"]["poi"] + 1),
    lambda d: d["districts"].update(unassigned_cells=d["districts"]["unassigned_cells"] + 1),
    lambda d: _district(d, "mock_c").update(outline=_district(d, "mock_a")["outline"]),
    lambda d: _district(d, "mock_a").update(outline=[]),
    lambda d: _district(d, "mock_b").update(counts=None),
    lambda d: _district(d, "mock_a").update(boundary_date=None),
    lambda d: _district(d, "mock_a")["name"].pop("kk"),
    lambda d: _district(d, "mock_a")["outline"][0].pop(),  # ring not closed
    lambda d: _district(d, "mock_a").update(status="guess"),
    lambda d: _district(d, "mock_a").update(rating=5),  # no rating of districts
    lambda d: d["districts"].update(official_list_url="http://example.org/akimat"),
    lambda d: d["districts"].update(official_list_checked=None),
    lambda d: d["districts"]["items"].append(dict(_district(d, "mock_c"))),  # duplicate id
    lambda d: d.pop("districts"),  # cells still name districts
])
def test_city_gaps_districts_reject(break_it) -> None:
    doc = _gaps()
    break_it(doc)
    with pytest.raises(ValidationError):
        CityGaps.model_validate(doc)


# ------------------------------------------------- public demo settings (final package, T161)


def test_public_demo_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.config import get_settings

    names = ("QADAM_RATE_PER_MIN", "QADAM_RATE_PER_DAY", "QADAM_TRUSTED_PROXY_HOPS",
             "LLM_DAILY_BUDGET_USD", "LLM_PRICE_IN_PER_MTOK", "LLM_PRICE_OUT_PER_MTOK")
    for name in names:
        monkeypatch.delenv(name, raising=False)
    get_settings.cache_clear()
    s = get_settings()
    assert (s.rate_per_min, s.rate_per_day, s.trusted_proxy_hops) == (0, 0, 0)  # off by default
    assert s.llm_daily_budget_usd is None and s.llm_budget_state == "none"
    monkeypatch.setenv("QADAM_RATE_PER_MIN", "20")
    monkeypatch.setenv("QADAM_TRUSTED_PROXY_HOPS", "1")
    monkeypatch.setenv("LLM_DAILY_BUDGET_USD", "2.5")
    get_settings.cache_clear()
    s = get_settings()
    assert (s.rate_per_min, s.trusted_proxy_hops, s.llm_daily_budget_usd) == (20, 1, 2.5)
    assert s.llm_budget_state == "misconfigured"  # a budget without prices: no Claude calls
    monkeypatch.setenv("LLM_PRICE_IN_PER_MTOK", "3")
    monkeypatch.setenv("LLM_PRICE_OUT_PER_MTOK", "15")
    get_settings.cache_clear()
    assert get_settings().llm_budget_state == "set"
    monkeypatch.setenv("QADAM_RATE_PER_DAY", "-1")
    get_settings.cache_clear()
    with pytest.raises(ValueError):
        get_settings()
    get_settings.cache_clear()


def test_image_turns_the_rate_limits_on() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "QADAM_RATE_PER_MIN=20" in dockerfile and "QADAM_RATE_PER_DAY=300" in dockerfile
