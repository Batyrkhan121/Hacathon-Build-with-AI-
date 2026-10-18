"""pipeline.overrides: hand corrections on top of OSM records."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.schemas import POI
from pipeline.overrides import apply_overrides

SPHERE = POI.model_validate({
    "id": "osm:way/454857501", "name": "EXPO (Нур Алем)", "category": "attraction",
    "tags": ["architecture", "photo_spot"], "geo": {"lat": 51.08925, "lon": 71.41619},
    "sources": ["osm", "manual"], "data_confidence": 0.5, "fetched_at": "2026-10-18",
})


def test_sphere_becomes_a_museum_with_hours() -> None:
    change = {"category": "museum", "opening_hours": "Tu-Su 10:00-18:30; Mo off",
              "source": "https://example.kz/nur-alem", "note": "for people", "rating": 5}
    (out,), notes = apply_overrides([SPHERE], {SPHERE.id: change})
    assert out.category == "museum" and out.indoor is True
    assert out.opening_hours == "Tu-Su 10:00-18:30; Mo off"
    assert set(out.tags) == {"architecture", "photo_spot", "indoor"}
    assert out.website == "https://example.kz/nur-alem"
    assert out.rating is None  # not an overridable field
    assert out.sources == ["osm", "manual"]
    assert out.data_confidence == 0.75  # + hours 0.15 + website 0.1
    assert notes == ["Ручные правки (data/manual/overrides.json): 1 мест."]


def test_unknown_id_and_bad_category() -> None:
    out, notes = apply_overrides([SPHERE], {"osm:node/1": {"category": "museum"}})
    assert out == [SPHERE] and "Не найдены id: osm:node/1" in notes[0]
    with pytest.raises(ValidationError):
        apply_overrides([SPHERE], {SPHERE.id: {"category": "spaceport"}})
    assert apply_overrides([SPHERE], {}) == ([SPHERE], [])


def test_hours_without_source_are_not_applied() -> None:
    change = {"category": "museum", "opening_hours": "Tu-Su 10:00-18:30; Mo off",
              "source": None}
    (out,), notes = apply_overrides([SPHERE], {SPHERE.id: change})
    assert out.category == "museum" and out.opening_hours is None
    assert out.data_confidence == 0.5  # no bonus for hours that were not applied
    assert "Часы и описания без ссылки на источник не применены: osm:way/454857501" in notes[0]


def test_category_fix_brings_its_tags_and_flag_only_entry_changes_nothing() -> None:
    kfc = POI.model_validate({
        "id": "osm:way/242373803", "name": "KFC", "category": "mall",
        "tags": ["indoor", "shopping"], "geo": {"lat": 51.1, "lon": 71.4}, "indoor": True,
        "sources": ["osm"], "data_confidence": 0.3, "fetched_at": "2026-10-18",
    })
    flag_only = SPHERE.model_copy(update={"id": "osm:node/2388707906", "sources": ["osm"]})
    out, _ = apply_overrides([kfc, flag_only], {
        kfc.id: {"category": "fast_food"},
        flag_only.id: {"generated_description": False, "note": "x"},
    })
    fixed, same = out
    assert fixed.category == "fast_food" and set(fixed.tags) == {"indoor", "food"}
    assert fixed.indoor is True and "manual" in fixed.sources
    assert same == flag_only  # no field changed, no "manual" source added


def test_full_record_from_an_official_source() -> None:
    change = {
        "name": "Alem.ai (бывш. Нур Алем)",
        "name_i18n": {"ru": "Alem.ai (бывш. Нур Алем)", "en": "Alem.ai (formerly Nur Alem)"},
        "category": "museum", "opening_hours": "Tu-Su 10:00-18:30; Mo off",
        "website": "https://alem.ai/", "tags_add": ["interactive", "kids", "not_a_tag"],
        "kids_friendly": True, "description": "Музей Сингулярности. Билет 2 500 ₸.",
        "sources_add": ["wikidata"], "source": "https://alem.ai/",
    }
    (out,), _ = apply_overrides([SPHERE], {SPHERE.id: change})
    assert out.name == "Alem.ai (бывш. Нур Алем)" and out.name_i18n["en"].startswith("Alem.ai")
    assert out.opening_hours == "Tu-Su 10:00-18:30; Mo off" and out.website == "https://alem.ai/"
    assert {"interactive", "kids", "indoor"} <= set(out.tags) and "not_a_tag" not in out.tags
    assert out.kids_friendly is True
    assert out.description_source == "manual" and out.sources == ["osm", "manual", "wikidata"]

    # the same description without a source link is not applied
    no_link = {k: v for k, v in change.items() if k != "source"}
    (bare,), notes = apply_overrides([SPHERE], {SPHERE.id: no_link})
    assert bare.description is None and bare.opening_hours is None
    assert "Часы и описания без ссылки" in notes[0]
