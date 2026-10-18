"""Hand corrections on top of OSM: data/manual/overrides.json.

    {"osm:way/454857501": {"category": "museum",
                           "opening_hours": "Tu-Su 10:00-18:30; Mo off",
                           "source": "https://...", "note": "why"}}

Only these fields can be overridden: name, category, opening_hours, address, website, phone.
`source` (a link) becomes `website` when the POI has none; `note` is for people only.
`opening_hours` is applied only together with a `source` link: unconfirmed hours could
make a card say "open now". A corrected category brings its category tags (food,
shopping, ...) and indoor default. {"generated_description": false} alone changes no field:
it only keeps pipeline.enrich from writing an LLM sentence for that place.
An overridden POI gets "manual" in `sources`; tags derived from category and hours
(indoor/outdoor, late_open, 24_7) are recomputed; data_confidence gains the bonus for
fields it did not have before. Every record is validated against app.schemas.POI.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path

from app.config import get_settings
from app.schemas import POI
from pipeline.normalize import hours_flags, indoor_flag

FIELDS = frozenset({"name", "category", "opening_hours", "address", "website", "phone"})
HOURS_TAGS = frozenset({"late_open", "24_7"})
# tags normalize derives from the category alone; they follow a corrected category
CATEGORY_TAG: dict[str, str] = {
    "mall": "shopping", "restaurant": "food", "fast_food": "food", "sport": "sport",
    "park": "walk", "bar": "night", "nightlife": "night", "cafe": "coffee", "gallery": "art",
}
# {"generated_description": false}: keep this place without an LLM sentence
NO_GENERATED = "generated_description"


def overrides_path() -> Path:
    return get_settings().data_dir / "manual" / "overrides.json"


def load_overrides(path: Path | None = None) -> dict[str, dict]:
    path = path or overrides_path()
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def has_source(change: Mapping[str, object]) -> bool:
    source = change.get("source")
    return isinstance(source, str) and source.startswith(("http://", "https://"))


def apply_one(poi: POI, change: Mapping[str, object]) -> POI:
    updates = {k: v for k, v in change.items() if k in FIELDS}
    if not has_source(change):
        # unconfirmed hours could make a card say "open now": never without a source link
        updates.pop("opening_hours", None)
    elif not (updates.get("website") or poi.website):
        updates["website"] = change["source"]
    data = poi.model_dump() | updates
    category = data["category"]
    tags = (set(poi.tags) - HOURS_TAGS) | hours_flags(data.get("opening_hours"))
    indoor = poi.indoor
    if category != poi.category:  # follow the corrected category, keep data-based values
        tags.discard(CATEGORY_TAG.get(poi.category, ""))
        if category in CATEGORY_TAG:
            tags.add(CATEGORY_TAG[category])
        indoor = indoor_flag(category, "")
        if indoor is None:
            indoor = poi.indoor
        tags -= {"indoor", "outdoor"}
        if indoor is True:
            tags.add("indoor")
        elif indoor is False:
            tags.add("outdoor")
    bonus = 0.0
    if data.get("opening_hours") and not poi.opening_hours:
        bonus += 0.15
    if data.get("address") and not poi.address:
        bonus += 0.10
    if (data.get("website") or data.get("phone")) and not (poi.website or poi.phone):
        bonus += 0.10
    data.update(
        tags=sorted(tags),
        indoor=indoor,
        sources=poi.sources if "manual" in poi.sources else [*poi.sources, "manual"],
        data_confidence=round(min(poi.data_confidence + bonus, 1.0), 2),
    )
    return POI.model_validate(data)


def apply_overrides(
    pois: Sequence[POI], overrides: Mapping[str, Mapping[str, object]]
) -> tuple[list[POI], list[str]]:
    if not overrides:
        return list(pois), []
    by_id = {p.id: p for p in pois}
    unknown = sorted(set(overrides) - set(by_id))
    out = [
        apply_one(p, overrides[p.id]) if FIELDS & set(overrides.get(p.id, {})) else p
        for p in pois
    ]
    applied = len(overrides) - len(unknown)
    note = f"Ручные правки (data/manual/overrides.json): {applied} мест."
    if unknown:
        note += f" Не найдены id: {', '.join(unknown)}."
    no_source = sorted(
        pid for pid, ch in overrides.items() if "opening_hours" in ch and not has_source(ch)
    )
    if no_source:
        note += f" Часы без ссылки на источник не применены: {', '.join(no_source)}."
    return out, [note]


def no_generated_ids(overrides: Mapping[str, Mapping[str, object]]) -> frozenset[str]:
    """Places that must not get an LLM sentence (a wrong guess is worse than none)."""
    return frozenset(pid for pid, ch in overrides.items() if ch.get(NO_GENERATED) is False)
