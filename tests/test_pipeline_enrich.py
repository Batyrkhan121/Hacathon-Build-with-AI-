"""pipeline.enrich with fake Wikidata and fake Claude (no network, no API key)."""

from __future__ import annotations

from pathlib import Path

from app.schemas import POI
from pipeline.enrich import (
    PlaceDescription,
    clean_sentence,
    llm_step,
    wikidata_step,
)


def poi(i: int, popularity: float = 0.1, **kw: object) -> POI:
    base = {
        "id": f"osm:node/{i}",
        "name": f"Место {i}",
        "category": "museum",
        "tags": ["indoor"],
        "geo": {"lat": 51.1, "lon": 71.4},
        "sources": ["osm"],
        "data_confidence": 0.5,
        "popularity": popularity,
        "fetched_at": "2026-10-18",
    }
    return POI.model_validate(base | kw)


def test_wikidata_fills_missing_descriptions_and_caches(tmp_path: Path) -> None:
    pois = [poi(1), poi(2, description="Из OSM", description_source="osm"), poi(3)]
    qids = {"osm:node/1": "Q1", "osm:node/2": "Q2", "osm:node/3": "Q3"}
    asked: list[list[str]] = []

    def fake_fetch(ids: list[str]) -> dict[str, str]:
        asked.append(ids)
        return {"Q1": "музей в Астане"}

    cache = tmp_path / "wd.json"
    out, note = wikidata_step(pois, qids, cache, fetch=fake_fetch)
    assert asked == [["Q1", "Q3"]]  # Q2 already has an OSM description
    assert out[0].description == "музей в Астане" and out[0].description_source == "wikidata"
    assert out[0].sources == ["osm", "wikidata"]
    assert out[1].description == "Из OSM"
    assert out[2].description is None
    assert "1 местам" in note

    asked.clear()
    wikidata_step(pois, qids, cache, fetch=fake_fetch)
    assert asked == []  # Q3 cached as "no description", not asked again


def test_wikidata_network_failure_is_a_note(tmp_path: Path) -> None:
    def down(ids: list[str]) -> dict[str, str]:
        raise OSError("no route")

    out, note = wikidata_step([poi(1)], {"osm:node/1": "Q1"}, tmp_path / "wd.json", fetch=down)
    assert out[0].description is None and "недоступна" in note


def test_llm_top_n_by_popularity_with_validation(tmp_path: Path) -> None:
    pois = [poi(1, 0.9), poi(2, 0.5), poi(3, 0.1), poi(4, 0.8, description="есть",
                                                     description_source="osm")]
    seen: list[list[dict]] = []

    def fake(items: list[dict]) -> list[PlaceDescription]:
        seen.append(items)
        return [
            PlaceDescription(poi_id="osm:node/1", text="Музей современного искусства."),
            PlaceDescription(poi_id="osm:node/2", text="Открыто с 10:00, билет 2000 тенге."),
            PlaceDescription(poi_id="osm:node/999", text="Чужой id."),
        ]

    cache = tmp_path / "desc.json"
    out, note = llm_step(pois, 2, cache, "fake-model", describe=fake)
    assert [it["poi_id"] for it in seen[0]] == ["osm:node/1", "osm:node/2"]
    assert set(seen[0][0]) == {"poi_id", "name", "category", "tags"}
    by_id = {p.id: p for p in out}
    assert by_id["osm:node/1"].description_source == "llm_generated"
    assert by_id["osm:node/1"].sources == ["osm"]  # generated text is not a fact source
    assert by_id["osm:node/2"].description is None  # hours/price text rejected
    assert by_id["osm:node/4"].description == "есть"
    assert "osm:node/999" not in by_id
    assert "отклонено проверкой: 1" in note

    seen.clear()
    llm_step(pois, 2, cache, "fake-model", describe=fake)
    assert [it["poi_id"] for it in seen[0]] == ["osm:node/2"]  # only the uncached one


def test_llm_error_keeps_going(tmp_path: Path) -> None:
    def broken(items: list[dict]) -> list[PlaceDescription]:
        raise RuntimeError("overloaded")

    out, note = llm_step([poi(1)], 5, tmp_path / "d.json", "m", describe=broken)
    assert out[0].description is None and "RuntimeError" in note


def test_llm_skipped_without_key(tmp_path: Path, monkeypatch) -> None:
    from app import config

    monkeypatch.setattr(config.Settings, "has_llm", property(lambda self: False))
    out, note = llm_step([poi(1)], 5, tmp_path / "d.json", "m")
    assert out[0].description is None and "ANTHROPIC_API_KEY" in note


def test_clean_sentence() -> None:
    assert clean_sentence("  Тихий   парк у воды. ") == "Тихий парк у воды."
    assert clean_sentence("Работает круглосуточно.") is None
    assert clean_sentence("Высокий рейтинг гостей.") is None
    assert clean_sentence("х" * 300) is None
