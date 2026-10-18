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
