"""Raw Overpass elements -> app.schemas.POI (categories, tags, flags, confidence, dedup).

    python -m pipeline.normalize --area fixture --out tests/fixtures/poi_sample.jsonl
    python -m pipeline.normalize --area astana          # -> data/processed/poi.jsonl
    python -m pipeline.normalize --area zones           # demo zones only

Rules are documented in .claude/skills/poi-data-pipeline/SKILL.md (later in docs/DATA.md).
A tag from TAG_VOCAB is set only when OSM data supports it, never by guess.
"""

from __future__ import annotations

import argparse
import json
import math
import re
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from pathlib import Path

import h3

from app.config import get_settings
from app.hours import ASTANA_TZ
from app.schemas import POI, TAG_VOCAB, Category

PLAYGROUND_NAME = "Детская площадка"
PLAYGROUND_MAX_CONFIDENCE = 0.4
DEDUP_RADIUS_M = 60.0
MAX_TAG_COUNT_FOR_POPULARITY = 30

AMENITY_CATEGORY: dict[str, Category] = {
    "cafe": "cafe",
    "ice_cream": "cafe",
    "restaurant": "restaurant",
    "food_court": "restaurant",
    "fast_food": "fast_food",
    "bar": "bar",
    "pub": "bar",
    "nightclub": "nightlife",
    "theatre": "theatre",
    "cinema": "cinema",
    "library": "library",
    "arts_centre": "gallery",
    "place_of_worship": "religious",
    "coworking_space": "other",
}
# tourism=attraction / artwork are weak labels: they lose to any other matching key
TOURISM_STRONG: dict[str, Category] = {
    "museum": "museum",
    "gallery": "gallery",
    "viewpoint": "viewpoint",
    "zoo": "attraction",
    "aquarium": "attraction",
    "theme_park": "attraction",
}
TOURISM_WEAK: dict[str, Category] = {"artwork": "monument", "attraction": "attraction"}
LEISURE_CATEGORY: dict[str, Category] = {
    "park": "park",
    "garden": "park",
    "playground": "playground",
    "water_park": "attraction",
    "sports_centre": "sport",
    "ice_rink": "sport",
    "swimming_pool": "sport",
    "amusement_arcade": "entertainment",
    "escape_game": "entertainment",
    "bowling_alley": "entertainment",
    "trampoline_park": "entertainment",
}
HISTORIC_VALUES = frozenset({"monument", "memorial", "building", "castle"})

INDOOR_CATEGORIES: frozenset[str] = frozenset(
    {
        "cafe", "restaurant", "fast_food", "bar", "nightlife", "museum", "gallery", "theatre",
        "cinema", "library", "religious", "mall", "entertainment",
    }
)
OUTDOOR_CATEGORIES: frozenset[str] = frozenset({"park", "playground", "viewpoint", "monument"})
INDOOR_VALUES = frozenset({"aquarium", "sports_centre", "coworking_space"})
OUTDOOR_VALUES = frozenset({"zoo", "theme_park"})
INDOOR_TAG_VALUES = frozenset({"yes", "room", "area", "corridor"})
# «в тепле»: indoor places anyone may walk into to warm up, by OSM category: shop=mall,
# tourism=museum, amenity=library|cafe|theatre|cinema (amenity=ice_cream is "cafe" here too).
# Narrower than indoor: restaurants and bars want an order, places of worship are not for it
WARM_CATEGORIES: frozenset[str] = frozenset(
    {"mall", "museum", "library", "cafe", "theatre", "cinema"}
)
KIDS_VALUES = frozenset({"playground", "zoo", "aquarium", "theme_park", "water_park"})
LOCAL_CUISINES = frozenset({"kazakh", "central_asian", "uzbek", "uyghur"})
HISTORY_MUSEUMS = frozenset({"history", "local", "military", "archaeological", "historic"})

MIN_PREFIX_LEN = 5
PLAYGROUND_NEAR_M = 150.0
LANDMARK_NEAR_M = 150.0
# words that name a kind of place, not a place: "у Сквер" is not a usable anchor
GENERIC_NAME_WORDS: frozenset[str] = frozenset(
    {
        # ru
        "сквер", "парк", "сад", "аллея", "бульвар", "площадь", "фонтан", "кафе", "кофейня",
        "ресторан", "столовая", "бар", "паб", "фастфуд", "шаурма", "пиццерия", "магазин",
        "трц", "тц", "торговый", "центр", "памятник", "монумент", "мечеть", "церковь", "храм",
        "библиотека", "кинотеатр", "театр", "музей", "галерея", "спортзал", "стадион",
        "бассейн", "каток", "детская", "площадка", "двор", "зона", "отдыха", "и", "на", "у",
        # kk
        "саябақ", "саябағы", "бақ", "бағы", "сквері", "алаң", "алаңы", "кафесі", "мейрамхана",
        "мейрамханасы", "дәмхана", "мешіт", "мешіті", "ескерткіш", "ескерткіші", "мұражай",
        "мұражайы", "кітапхана", "кітапханасы", "ойын", "балалар", "алаңшасы",
        # en
        "park", "garden", "square", "cafe", "coffee", "restaurant", "bar", "pub", "mall",
        "museum", "monument", "mosque", "church", "library", "cinema", "theatre", "gym",
        "playground", "the", "of",
    }
)
# landmarks are curated by the team (source "manual"): famous buildings worth a photo
LANDMARK_TAGS: tuple[str, ...] = ("architecture", "photo_spot")

_TIME_RANGE = re.compile(r"(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})")
_NON_WORD = re.compile(r"[\W_]+")


# ---------------------------------------------------------------------- helpers


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    # local copy: app.geo belongs to the CORE lane and may still be a stub
    r = 6_371_000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def element_point(el: Mapping) -> tuple[float, float] | None:
    """Node: lat/lon. Way/relation: `center` from `out center`."""
    if "lat" in el and "lon" in el:
        return float(el["lat"]), float(el["lon"])
    center = el.get("center")
    if center and "lat" in center and "lon" in center:
        return float(center["lat"]), float(center["lon"])
    return None


def osm_kind(tags: Mapping[str, str]) -> tuple[Category, str] | None:
    """(category, matched OSM value) by priority: strong tourism > mall > amenity > leisure >
    historic > weak tourism. None when the element is outside our selectors."""
    tourism = tags.get("tourism", "")
    if tourism in TOURISM_STRONG:
        return TOURISM_STRONG[tourism], tourism
    if tags.get("shop") == "mall":
        return "mall", "mall"
    amenity = tags.get("amenity", "")
    if amenity in AMENITY_CATEGORY:
        return AMENITY_CATEGORY[amenity], amenity
    leisure = tags.get("leisure", "")
    if leisure in LEISURE_CATEGORY:
        return LEISURE_CATEGORY[leisure], leisure
    if tags.get("historic", "") in HISTORIC_VALUES:
        return "monument", "historic"
    if tourism in TOURISM_WEAK:
        return TOURISM_WEAK[tourism], tourism
    return None


def hours_flags(opening_hours: str | None) -> set[str]:
    """`24_7` and `late_open` (some day closes at 23:00 or later, or past midnight)."""
    if not opening_hours:
        return set()
    oh = opening_hours.strip()
    if oh in {"24/7", "Mo-Su 00:00-24:00", "00:00-24:00"}:
        return {"24_7", "late_open"}
    for m in _TIME_RANGE.finditer(oh):
        start = int(m.group(1)) * 60 + int(m.group(2))
        end = int(m.group(3)) * 60 + int(m.group(4))
        if end >= 23 * 60 or end < start:
            return {"late_open"}
    return set()


def vocab_tags(tags: Mapping[str, str], category: Category, value: str) -> list[str]:
    out: set[str] = set()
    cuisine = {c.strip() for c in tags.get("cuisine", "").split(";") if c.strip()}

    if category == "cafe" or "coffee_shop" in cuisine or "coffee" in cuisine:
        out.add("coffee")
    if tags.get("internet_access") in {"wlan", "yes", "wifi"}:
        out.add("wifi")
    if tags.get("power_supply") == "yes":
        out.add("power_outlets")
    if value in KIDS_VALUES or tags.get("kids_area") == "yes":
        out.add("kids")
    out |= hours_flags(tags.get("opening_hours"))
    if category in {"bar", "nightlife"}:
        out.add("night")
    if category == "viewpoint" or tags.get("tower:type") == "observation":
        out.add("view")
    if tags.get("fee") == "no":
        out.add("free")
    if tags.get("diet:vegetarian") in {"yes", "only"}:
        out.add("vegetarian")
    if tags.get("wheelchair") == "yes":
        out.add("wheelchair")
    if category == "gallery" or value == "artwork" or tags.get("museum") == "art":
        out.add("art")
    if value == "historic" or "historic" in tags or tags.get("museum") in HISTORY_MUSEUMS:
        out.add("history")
    if category in {"restaurant", "fast_food"}:
        out.add("food")
    if cuisine & LOCAL_CUISINES:
        out.add("local_cuisine")
    if tags.get("live_music") == "yes":
        out.add("live_music")
    if category == "mall":
        out.add("shopping")
    if category == "sport":
        out.add("sport")
    if category == "park":
        out.add("walk")
    if value == "coworking_space":
        out |= {"work_friendly", "wifi"}

    indoor = indoor_flag(category, value, tags)
    if indoor is True:
        out.add("indoor")
    elif indoor is False:
        out.add("outdoor")
    return sorted(out & TAG_VOCAB)


def indoor_flag(
    category: Category, value: str, tags: Mapping[str, str] | None = None
) -> bool | None:
    if value in INDOOR_VALUES or category in INDOOR_CATEGORIES:
        return True
    if value in OUTDOOR_VALUES or category in OUTDOOR_CATEGORIES:
        return False
    if tags and tags.get("indoor") in INDOOR_TAG_VALUES:  # explicit OSM indoor mapping
        return True
    return None


def address_of(tags: Mapping[str, str]) -> str | None:
    if tags.get("addr:full"):
        return tags["addr:full"]
    street, house = tags.get("addr:street"), tags.get("addr:housenumber")
    if street and house:
        return f"{street}, {house}"
    return street or None


def data_confidence(
    *,
    has_hours: bool,
    has_contact: bool,
    has_description: bool,
    has_wikidata: bool,
    has_address: bool,
    multilingual: bool,
) -> float:
    """0.3 base + completeness bonuses, capped at 1.0 (formula goes to docs/DATA.md)."""
    score = 0.3
    score += 0.15 if has_hours else 0.0
    score += 0.10 if has_contact else 0.0
    score += 0.15 if has_description else 0.0
    score += 0.10 if has_wikidata else 0.0
    score += 0.10 if has_address else 0.0
    score += 0.10 if multilingual else 0.0
    return round(min(score, 1.0), 2)


def popularity(tags: Mapping[str, str], category: Category) -> float:
    """Documented proxy, NOT a rating: wiki links, tag richness, landmark-like category, brand."""
    score = 0.0
    if "wikidata" in tags or "wikipedia" in tags:
        score += 0.4
    score += 0.3 * min(len(tags) / MAX_TAG_COUNT_FOR_POPULARITY, 1.0)
    if category in {"attraction", "museum", "viewpoint"}:
        score += 0.2
    if "brand" in tags:
        score += 0.1
    return round(min(score, 1.0), 3)


# ------------------------------------------------------------------- normalize


def normalize_element(
    el: Mapping, fetched_at: str, stats: dict | None = None
) -> POI | None:
    """One OSM element -> POI, or None. `stats` (optional) counts why elements are dropped:
    no_category, no_point, unnamed; and wheelchair values outside yes/limited/no."""
    counts = stats if stats is not None else {}
    tags: Mapping[str, str] = el.get("tags") or {}
    kind = osm_kind(tags)
    point = element_point(el)
    if kind is None:
        counts["no_category"] = counts.get("no_category", 0) + 1
        return None
    if point is None:
        counts["no_point"] = counts.get("no_point", 0) + 1
        return None
    category, value = kind

    name = tags.get("name:ru") or tags.get("name")
    unnamed_playground = False
    if not name:
        if value != "playground":
            counts["unnamed"] = counts.get("unnamed", 0) + 1
            return None
        name, unnamed_playground = PLAYGROUND_NAME, True
    name_i18n = {
        lang: tags[f"name:{lang}"] for lang in ("ru", "kk", "en") if tags.get(f"name:{lang}")
    }

    description = tags.get("description:ru") or tags.get("description")
    address = address_of(tags)
    website = tags.get("website") or tags.get("contact:website")
    phone = tags.get("phone") or tags.get("contact:phone")
    opening_hours = tags.get("opening_hours")

    confidence = data_confidence(
        has_hours=bool(opening_hours),
        has_contact=bool(website or phone),
        has_description=bool(description),
        has_wikidata="wikidata" in tags,
        has_address=bool(address),
        multilingual=len(name_i18n) > 1,
    )
    if unnamed_playground:
        confidence = min(confidence, PLAYGROUND_MAX_CONFIDENCE)

    wheelchair = tags.get("wheelchair")
    if wheelchair and wheelchair not in {"yes", "limited", "no"}:  # e.g. "designated"
        other = counts.setdefault("wheelchair_other", {})
        other[wheelchair] = other.get(wheelchair, 0) + 1
    kids = value in KIDS_VALUES or tags.get("kids_area") == "yes"
    lat, lon = point
    return POI(
        id=f"osm:{el['type']}/{el['id']}",
        name=name,
        name_i18n=name_i18n,
        category=category,
        tags=vocab_tags(tags, category, value),
        geo={"lat": round(lat, 7), "lon": round(lon, 7)},
        h3_r9=h3.latlng_to_cell(lat, lon, 9),
        description=description,
        description_source="osm" if description else None,
        address=address,
        opening_hours=opening_hours,
        phone=phone,
        website=website,
        wheelchair=wheelchair if wheelchair in {"yes", "limited", "no"} else "unknown",
        kids_friendly=True if kids else None,
        indoor=indoor_flag(category, value, tags),
        popularity=popularity(tags, category),
        sources=["osm"],
        data_confidence=confidence,
        fetched_at=fetched_at,
    )


def name_keys(poi: POI) -> set[str]:
    """Casefolded name variants without spaces/punctuation: "Mega Silk Way" -> "megasilkway"."""
    names = [poi.name, *poi.name_i18n.values()]
    return {key for n in names if (key := _NON_WORD.sub("", n.casefold()))}


def same_name(a: set[str], b: set[str]) -> bool:
    """Any shared variant, or one variant extends another ("megasilkway" ~ "megasilkwayastana")."""
    if a & b:
        return True
    return any(
        min(len(x), len(y)) >= MIN_PREFIX_LEN and (x.startswith(y) or y.startswith(x))
        for x in a
        for y in b
    )


def dedupe(pois: Iterable[POI], radius_m: float = DEDUP_RADIUS_M) -> list[POI]:
    """Same name (any variant) closer than radius_m -> keep the higher data_confidence
    (ties: the first in id order, so the output is deterministic)."""
    kept: list[tuple[POI, set[str]]] = []
    for poi in sorted(pois, key=lambda p: (-p.data_confidence, p.id)):
        keys = name_keys(poi)
        if any(
            same_name(keys, other_keys)
            and haversine_m(poi.geo.lat, poi.geo.lon, other.geo.lat, other.geo.lon) < radius_m
            for other, other_keys in kept
        ):
            continue
        kept.append((poi, keys))
    return sorted((p for p, _ in kept), key=lambda p: p.id)


def osm_base(payload: Mapping) -> str:
    """ISO timestamp of the OSM database the mirror answered from ("" if unknown)."""
    return (payload.get("osm3s") or {}).get("timestamp_osm_base") or ""


def fetched_date(payload: Mapping) -> str:
    """Date of the data in Astana time: the OSM snapshot the mirror served (a lagging
    mirror must not look fresh), else our fetch stamp."""
    base = osm_base(payload)
    if base:
        osm_time = datetime.fromisoformat(base.replace("Z", "+00:00"))
        return osm_time.astimezone(ASTANA_TZ).date().isoformat()
    stamp = (payload.get("qadam") or {}).get("fetched_at_unix")
    if stamp:
        return datetime.fromtimestamp(stamp, tz=ASTANA_TZ).date().isoformat()
    return datetime.now(ASTANA_TZ).date().isoformat()


def coverage(payload: Mapping) -> list[str]:
    """Spatial filters the chunk was fetched with ("s,w,n,e" or "around:R,lat,lon")."""
    meta = payload.get("qadam") or {}
    if meta.get("filters"):
        return list(meta["filters"])
    if meta.get("area") == "fixture":  # fetched before chunks recorded their filters
        from pipeline.fetch_osm import area_chunks

        return list(area_chunks("fixture")[0].filters)
    return []


def covers(filters: Sequence[str], lat: float, lon: float) -> bool:
    for flt in filters:
        if flt.startswith("around:"):
            radius, clat, clon = (float(x) for x in flt[len("around:"):].split(","))
            if haversine_m(lat, lon, clat, clon) <= radius:
                return True
        else:
            s, w, n, e = (float(x) for x in flt.split(","))
            if s <= lat <= n and w <= lon <= e:
                return True
    return False


def normalize_payloads(
    payloads: Iterable[Mapping], stats: dict | None = None
) -> list[POI]:
    """Several raw chunks -> one POI list. Elements repeated across overlapping chunks are
    taken once, from the freshest OSM snapshot (mirrors lag: kumi served a June database
    in October). An element found only in an older snapshot whose point a fresher *complete*
    chunk also covers is stale (deleted or retagged since) and is dropped; a partial chunk
    (Overpass `remark`) never proves absence. Then near-duplicates are merged by `dedupe`.
    `stats["stale"]` counts dropped elements, `stats["stale_pois"]` the names of former POIs."""
    from pipeline.fetch_osm import is_partial

    ordered = sorted(payloads, key=osm_base, reverse=True)  # stable for equal stamps
    seen: set[str] = set()
    pois: list[POI] = []
    stale = 0
    stale_pois: list[str] = []
    drops: dict = {"no_category": 0, "no_point": 0, "unnamed": 0, "wheelchair_other": {}}
    for i, payload in enumerate(ordered):
        date, base = fetched_date(payload), osm_base(payload)
        fresher = [
            coverage(q) for q in ordered[:i] if osm_base(q) > base and not is_partial(q)
        ]
        for el in payload.get("elements", []):
            key = f"{el.get('type')}/{el.get('id')}"
            if key in seen:
                continue
            seen.add(key)
            point = element_point(el)
            if fresher and point and any(covers(f, *point) for f in fresher):
                stale += 1
                if gone := normalize_element(el, date):
                    stale_pois.append(f"{gone.name} ({gone.id})")
                continue
            if poi := normalize_element(el, date, drops):
                pois.append(poi)
    merged = dedupe(pois)
    if stats is not None:
        stats.update(drops)
        stats["stale"] = stale
        stats["stale_pois"] = stale_pois
        stats["duplicates"] = len(pois) - len(merged)
    return merged


def normalize_payload(payload: Mapping) -> list[POI]:
    return normalize_payloads([payload])


# ----------------------------------------------------------------- post-steps


def landmark_pois(
    pois: Sequence[POI],
    landmarks: Sequence[Mapping],
    osm_refs: Mapping[str, str],
    qids: Mapping[str, str],
    checked_at: str,
) -> list[POI]:
    """Verified, non-transport landmarks that our OSM selectors miss (the Pyramid is an
    amenity=community_centre, the Nur Alem sphere an office) become `attraction` POIs.
    A landmark whose OSM object, or a same-named POI nearby, is already present is skipped.
    Its description comes later from Wikidata (pipeline.enrich)."""
    present = {p.id for p in pois}
    out: list[POI] = []
    for lm in landmarks:
        ref = osm_refs.get(lm["id"])
        if lm.get("kind") == "transport" or not lm.get("verified") or ref is None:
            continue
        poi_id = f"osm:{ref}"
        lat, lon = float(lm["geo"]["lat"]), float(lm["geo"]["lon"])
        names = {lang: v for lang, v in lm["names"].items() if lang in ("ru", "kk", "en") and v}
        name = names.get("ru") or next(iter(names.values()))
        candidate_keys = {_NON_WORD.sub("", n.casefold()) for n in names.values()}
        if poi_id in present or any(
            contains_name(candidate_keys, name_keys(p))
            and haversine_m(lat, lon, p.geo.lat, p.geo.lon) < LANDMARK_NEAR_M
            for p in pois
        ):
            continue
        qid = qids.get(lm["id"])
        out.append(
            POI(
                id=poi_id,
                name=name,
                name_i18n=names,
                category="attraction",
                tags=list(LANDMARK_TAGS),
                geo={"lat": lat, "lon": lon},
                h3_r9=h3.latlng_to_cell(lat, lon, 9),
                popularity=popularity({"wikidata": qid} if qid else {}, "attraction"),
                sources=["osm", "manual"],
                data_confidence=data_confidence(
                    has_hours=False,
                    has_contact=False,
                    has_description=False,
                    has_wikidata=bool(qid),
                    has_address=False,
                    multilingual=len(names) > 1,
                ),
                fetched_at=checked_at,
            )
        )
    return out


def contains_name(a: set[str], b: set[str]) -> bool:
    """Looser than same_name, for landmark names with a type prefix:
    "трцханшатыр" contains "ханшатыр"."""
    return any(
        min(len(x), len(y)) >= MIN_PREFIX_LEN and (x in y or y in x) for x in a for y in b
    )


def is_unnamed_playground(poi: POI) -> bool:
    return poi.category == "playground" and poi.name == PLAYGROUND_NAME


def has_proper_name(name: str) -> bool:
    """A name of its own, not just a kind of place: "Сквер Акжайык", "Coffee Boom" yes;
    "Сквер", "Кафе 24", "саябақ" no (what is left after generic words has < 3 letters)."""
    words = re.findall(r"[^\W\d_]+", name.casefold())
    return sum(len(w) for w in words if w not in GENERIC_NAME_WORDS) >= 3


def name_playgrounds(pois: Sequence[POI], radius_m: float = PLAYGROUND_NEAR_M) -> list[POI]:
    """"Детская площадка" -> "Детская площадка у <nearest non-playground POI with a proper
    name>" when one is within radius_m; otherwise the generic name stays."""
    buckets: dict[str, list[POI]] = {}
    for p in pois:
        if p.category != "playground" and has_proper_name(p.name):
            buckets.setdefault(h3.latlng_to_cell(p.geo.lat, p.geo.lon, 8), []).append(p)
    out: list[POI] = []
    for p in pois:
        if not is_unnamed_playground(p):
            out.append(p)
            continue
        cell = h3.latlng_to_cell(p.geo.lat, p.geo.lon, 8)
        near = [
            (haversine_m(p.geo.lat, p.geo.lon, a.geo.lat, a.geo.lon), a.id, a)
            for c in h3.grid_disk(cell, 1)
            for a in buckets.get(c, [])
        ]
        near = [item for item in near if item[0] <= radius_m]
        if near:
            anchor = min(near, key=lambda item: (item[0], item[1]))[2]
            p = p.model_copy(update={"name": f"{PLAYGROUND_NAME} у {anchor.name}"})
        out.append(p)
    return out


def finalize(pois: Sequence[POI], landmarks: Sequence[Mapping]) -> list[POI]:
    """Steps after dedupe: landmarks as POIs, then playground names (landmarks can be
    their anchors)."""
    from pipeline.landmark_refs import LANDMARK_OSM, LANDMARK_WIKIDATA, LANDMARKS_CHECKED_AT

    extra = landmark_pois(pois, landmarks, LANDMARK_OSM, LANDMARK_WIKIDATA, LANDMARKS_CHECKED_AT)
    return sorted(name_playgrounds([*pois, *extra]), key=lambda p: p.id)


Edge = tuple[tuple[float, float], tuple[float, float]]
Footprint = tuple[str, tuple[float, float, float, float], list[Edge], bool]  # .., is building


def mall_footprints(payload: Mapping) -> list[Footprint]:
    """(mall id, bbox s/w/n/e, boundary edges) from an `out geom` answer. Multipolygon
    members are not joined into rings: the even-odd test below only needs the edges."""
    out = []
    for el in payload.get("elements", []):
        lines = [el.get("geometry") or []] if el.get("type") == "way" else [
            m.get("geometry") or [] for m in el.get("members", []) if m.get("type") == "way"
        ]
        edges: list[Edge] = [
            ((a["lat"], a["lon"]), (b["lat"], b["lon"]))
            for line in lines
            for a, b in zip(line, line[1:], strict=False)
        ]
        if not edges:
            continue
        lats = [pt[0] for e in edges for pt in e]
        lons = [pt[1] for e in edges for pt in e]
        bbox = (min(lats), min(lons), max(lats), max(lons))
        is_building = "building" in (el.get("tags") or {})
        out.append((f"osm:{el['type']}/{el['id']}", bbox, edges, is_building))
    return out


def inside(lat: float, lon: float, edges: Sequence[Edge]) -> bool:
    """Even-odd ray casting along the latitude line (holes count as boundaries too)."""
    crossings = 0
    for (lat1, lon1), (lat2, lon2) in edges:
        if (lat1 > lat) != (lat2 > lat):
            lon_at = lon1 + (lat - lat1) * (lon2 - lon1) / (lat2 - lat1)
            if lon < lon_at:
                crossings += 1
    return crossings % 2 == 1


def mark_inside_malls(pois: Sequence[POI], outlines: Mapping | None) -> tuple[list[POI], int]:
    """indoor=True for places whose point lies inside a mall footprint from OSM (e.g. rides
    inside Khan Shatyr). Inside a mall *building* this beats the category default (Fame City,
    a theme park, or a playground under the roof); inside a mall area that is not a building
    only an unknown value is filled."""
    if not outlines:
        return list(pois), 0
    malls = mall_footprints(outlines)
    out: list[POI] = []
    marked = 0
    for p in pois:
        if p.indoor is not True and p.category != "mall":
            lat, lon = p.geo.lat, p.geo.lon
            if any(
                (is_building or p.indoor is None)
                and s <= lat <= n
                and w <= lon <= e
                and inside(lat, lon, edges)
                for _, (s, w, n, e), edges, is_building in malls
            ):
                tags = sorted({*p.tags, "indoor"} - {"outdoor"})
                p = p.model_copy(update={"indoor": True, "tags": tags})
                marked += 1
        out.append(p)
    return out, marked


def load_outlines(raw_dir: Path) -> dict | None:
    path = raw_dir / "osm_mall_outlines.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def load_landmarks(path: Path) -> list[dict]:
    return json.loads(path.read_text(encoding="utf-8"))


def raw_files(area: str, raw_dir: Path) -> list[Path]:
    """Raw chunk files feeding an area: `astana` takes every chunk on disk (tiles, zones,
    fixture) so a partly downloaded city still has the demo zones."""
    patterns = {
        "fixture": ["osm_fixture.json"],
        "zones": ["osm_zone_*.json"],
        "astana": ["osm_astana_tile_*.json", "osm_zone_*.json", "osm_fixture.json"],
    }[area]
    return [p for pattern in patterns for p in sorted(raw_dir.glob(pattern))]


def load_payloads(paths: Iterable[Path]) -> list[dict]:
    return [json.loads(p.read_text(encoding="utf-8")) for p in paths]


def write_jsonl(pois: Sequence[POI], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as fh:
        for poi in pois:
            fh.write(json.dumps(poi.model_dump(mode="json", exclude_none=True), ensure_ascii=False))
            fh.write("\n")
    tmp.replace(path)


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--area", choices=["astana", "zones", "fixture"], default="astana")
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)
    settings = get_settings()
    out = args.out or settings.poi_path
    paths = raw_files(args.area, settings.data_dir / "raw")
    if not paths:
        raise SystemExit(f"no raw files for area {args.area!r}: run pipeline.fetch_osm first")
    pois = normalize_payloads(load_payloads(paths))
    if args.area != "fixture":  # the T10 test fixture stays plain OSM around EXPO/Mega
        pois = finalize(pois, load_landmarks(settings.landmarks_path))
        pois, _ = mark_inside_malls(pois, load_outlines(settings.data_dir / "raw"))
    write_jsonl(pois, out)
    print(f"{len(pois)} POI from {len(paths)} raw files -> {out}")


if __name__ == "__main__":
    main()
