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
        return {"Q1": "музей золота сарматов и древней истории Великой степи",
                "Q3": "музей в Астане, Казахстан"}  # short: empty for a card

    cache = tmp_path / "wd.json"
    out, note = wikidata_step(pois, qids, cache, fetch=fake_fetch)
    assert asked == [["Q1", "Q3"]]  # Q2 already has an OSM description
    assert out[0].description.startswith("музей золота сарматов")
    assert out[0].description_source == "wikidata"
    assert out[0].sources == ["osm", "wikidata"]
    assert out[1].description == "Из OSM"
    assert out[2].description is None  # the short Wikidata line was not taken
    assert "1 местам" in note and "не взяты: 1" in note

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
    assert set(seen[0][0]) == {"poi_id", "name", "other_names", "category", "tags"}
    by_id = {p.id: p for p in out}
    assert by_id["osm:node/1"].description_source == "llm_generated"
    assert by_id["osm:node/1"].sources == ["osm"]  # generated text is not a fact source
    assert by_id["osm:node/2"].description is None  # hours/price text rejected
    assert by_id["osm:node/4"].description == "есть"
    assert "osm:node/999" not in by_id
    assert "пересказ названия и тегов): 1" in note

    seen.clear()
    llm_step(pois, 2, cache, "fake-model", describe=fake)
    assert seen == []  # accepted and rejected answers are both cached: nothing re-asked


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


def test_only_notable_places_get_generated_text(tmp_path: Path) -> None:
    cafe = poi(70, 0.9, category="cafe", tags=["coffee"])
    playground = poi(71, 0.8, category="playground", tags=["kids"])
    park = poi(72, 0.1, category="park", tags=["walk"])
    old_bar = poi(73, 0.5, category="bar", description="Бар.", description_source="llm_generated")
    osm_cafe = poi(74, 0.5, category="cafe", description="Из OSM", description_source="osm")
    asked: list[str] = []

    def fake(items: list[dict]) -> list[PlaceDescription]:
        asked.extend(it["poi_id"] for it in items)
        return [PlaceDescription(poi_id=it["poi_id"],
                                 text="Парк разбит вокруг пруда с фонтанами и аллеей скульптур.")
                for it in items]

    out, note = llm_step([cafe, playground, park, old_bar, osm_cafe], None,
                         tmp_path / "d.json", "m", describe=fake)
    by_id = {p.id: p for p in out}
    assert asked == ["osm:node/72"]  # only the park, despite the lowest popularity
    assert by_id["osm:node/72"].description_source == "llm_generated"
    assert by_id["osm:node/70"].description is None
    assert by_id["osm:node/73"].description is None  # old generated text on a bar removed
    assert by_id["osm:node/74"].description == "Из OSM"  # data descriptions stay
    assert "Убрано прежних сгенерированных описаний: 1" in note
    assert "1 из 1 заметных мест" in note


def test_check_allows_plain_words() -> None:
    assert clean_sentence("Крупный торговый центр в центре города.")
    assert clean_sentence("Памятник «Астана Жұлдызы» — звезда на набережной.")
    assert clean_sentence("Сцена театра на набережной Есиля.")
    assert clean_sentence("Цена билета — 2000.") is None
    assert clean_sentence("Отель на 5 звёзд.") is None


def test_free_paid_and_star_words_are_rejected() -> None:
    assert clean_sentence("Музей, вход бесплатный согласно тегам.") is None
    assert clean_sentence("Платная парковка рядом.") is None
    assert clean_sentence("Пятизвёздочный отель у набережной.") is None


def test_skip_ids_and_malls_get_no_generated_text(tmp_path: Path) -> None:
    gallery = poi(100, 0.5, category="gallery")
    mall = poi(101, 0.9, category="mall", tags=["shopping"])
    old = poi(102, 0.5, category="gallery", description="Галерея.",
              description_source="llm_generated")
    asked: list[str] = []

    def fake(items: list[dict]) -> list[PlaceDescription]:
        asked.extend(it["poi_id"] for it in items)
        return [PlaceDescription(poi_id=it["poi_id"], text="Описание.") for it in items]

    out, _ = llm_step([gallery, mall, old], None, tmp_path / "d.json", "m", describe=fake,
                      skip_ids=frozenset({"osm:node/100", "osm:node/102"}))
    assert asked == []  # gallery skipped by override, mall is not described any more
    assert all(p.description is None for p in out)


def test_cache_only_mode_applies_cache_without_asking(tmp_path: Path, monkeypatch) -> None:
    from app import config

    cache = tmp_path / "d.json"
    museum = poi(110, 0.5)
    llm_step([museum], None, cache, "m",
             describe=lambda items: [PlaceDescription(poi_id=items[0]["poi_id"],
                                                      text="Небольшой музей истории.")])
    monkeypatch.setattr(config.Settings, "has_llm", property(lambda self: False))
    (out,), note = llm_step([museum, poi(111, 0.4)][:1], None, cache, "m")
    assert out.description == "Небольшой музей истории." and "только из кэша" in note


def test_restating_name_category_tags_is_kept_as_null(tmp_path: Path) -> None:
    seifullin = poi(120, 0.6, name="Музей Сакена Сейфуллина", tags=["indoor"])
    baiterek = poi(121, 0.9, name="Байтерек", category="monument", tags=["history", "outdoor"])
    unknown = poi(122, 0.1, name="Неизвестный памятник", category="monument")

    def fake(items: list[dict]) -> list[PlaceDescription]:
        answers = {
            "osm:node/120": "Музей Сакена Сейфуллина — музей, расположенный в помещении.",
            "osm:node/121": "Монумент в честь переноса столицы с панорамой Левого берега "
                            "с высоты 97 метров.",
            "osm:node/122": None,  # the model is not sure
        }
        return [PlaceDescription(poi_id=it["poi_id"], text=answers[it["poi_id"]])
                for it in items]

    cache = tmp_path / "d.json"
    out, note = llm_step([seifullin, baiterek, unknown], None, cache, "m", describe=fake)
    by_id = {p.id: p for p in out}
    assert by_id["osm:node/120"].description is None  # restatement -> null
    assert by_id["osm:node/121"].description.startswith("Монумент в честь")
    assert by_id["osm:node/122"].description is None
    assert "модель не уверена (null): 1" in note and "пересказ названия" in note

    asked: list[str] = []
    llm_step([unknown], None, cache, "m",
             describe=lambda items: asked.extend(i["poi_id"] for i in items) or [])
    assert asked == []  # a declined place is not asked again


def test_amenities_and_addresses_are_rejected() -> None:
    assert clean_sentence("Уютное кафе с Wi-Fi и парковкой.") is None
    assert clean_sentence("Памятник стоит на проспекте Республики.") is None
    assert clean_sentence("Здание по ул. Бейбитшилик, д. 5.") is None


def test_unanswered_places_are_counted(tmp_path: Path) -> None:
    pois = [poi(130, 0.5), poi(131, 0.4), poi(132, 0.3)]

    def partial(items: list[dict]) -> list[PlaceDescription]:  # one answer plus junk
        return [PlaceDescription(poi_id="osm:node/130", text="Музей древних рукописей и печатей."),
                PlaceDescription(poi_id="osm:node/999", text="Чужой.")]

    out, note = llm_step(pois, None, tmp_path / "d.json", "m", describe=partial)
    assert "Без ответа модели (отказ, пусто или чужие id): 2" in note
    asked: list[str] = []
    llm_step(pois, None, tmp_path / "d.json", "m",
             describe=lambda items: asked.extend(i["poi_id"] for i in items) or [])
    assert asked == ["osm:node/131", "osm:node/132"]  # unanswered ones are asked again


def test_wikidata_cache_ttl_and_refresh(tmp_path: Path) -> None:
    import json
    import time

    cache = tmp_path / "wd.json"
    now = time.time()
    cache.write_text(json.dumps({
        "Q1": {"text": "", "at": now - 8 * 86400},  # empty and 8 days old: ask again
        "Q2": {"text": "", "at": now - 86400},  # empty but fresh: keep
        "Q3": "старый формат строкой",  # legacy entry: read as fresh
    }), encoding="utf-8")
    pois = [poi(1), poi(2), poi(3)]
    qids = {"osm:node/1": "Q1", "osm:node/2": "Q2", "osm:node/3": "Q3"}
    asked: list[list[str]] = []

    def fetch(ids: list[str]) -> dict[str, str]:
        asked.append(ids)
        return {}

    wikidata_step(pois, qids, cache, fetch=fetch)
    assert asked == [["Q1"]]
    asked.clear()
    wikidata_step(pois, qids, cache, fetch=fetch, refresh=True)
    assert asked == [["Q1", "Q2", "Q3"]]
