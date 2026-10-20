"""Qadam — data contract between all layers.

Owner: INTEGRATOR (main branch only; in wave 8 the wave-8 branch). Lanes must NOT edit this file.
Need a change? Put a "CONTRACT CHANGE REQUEST" into your lane report; the integrator
applies it on main (wave 8: on wave-8) and tells the lanes which commit to merge.

Flow: pipeline -> data/processed/poi.jsonl (POI) -> store/retrieval (Candidate)
      -> generate (DraftResponse) -> validate (Card) -> pipeline (Answer) -> api -> web/eval
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

SCHEMA_VERSION = "1.3.0"
# 1.1.0: walking route on the OSM walk graph; 1.2.0: cells up to r10;
# 1.3.0 (wave 8): route_profile, prefs and exclude_poi_ids in the request, route profile facts,
# event cards, crossing kind unknown
MAX_H3_RES = 10  # finest H3 cell accepted (r10 ≈ 1,8 га in Astana); finer = near-exact point

Lang = Literal["ru", "kk", "en"]

Category = Literal[
    "cafe",
    "restaurant",
    "fast_food",
    "bar",
    "nightlife",
    "museum",
    "gallery",
    "theatre",
    "cinema",
    "library",
    "park",
    "playground",
    "viewpoint",
    "attraction",
    "monument",
    "religious",
    "mall",
    "sport",
    "entertainment",
    "other",
]

# Controlled tag vocabulary: pipeline, retrieval, LLM prompts and UI use ONLY these.
TAG_VOCAB: frozenset[str] = frozenset(
    {
        "coffee",
        "wifi",
        "power_outlets",
        "quiet",
        "work_friendly",
        "kids",
        "interactive",
        "indoor",
        "outdoor",
        "view",
        "photo_spot",
        "night",
        "late_open",
        "24_7",
        "free",
        "wheelchair",
        "vegetarian",
        "local_cuisine",
        "live_music",
        "walk",
        "water",
        "architecture",
        "history",
        "art",
        "shopping",
        "sport",
        "food",
    }
)

Source = Literal[
    "osm",
    "wikidata",
    "wikipedia",
    "google_places",
    "manual",
    "llm_generated",
    "open_meteo",
    "mock",
]
OpenStatus = Literal["open", "closed", "unknown"]
Confidence = Literal["high", "medium", "low"]
Wheelchair = Literal["yes", "limited", "no", "unknown"]
RouteProfile = Literal["fastest", "accessible", "lit"]  # быстрее / без ступенек / по освещённым

# 1.3.0: fixed preference vocabulary. No free text: an unknown tag is a 422.
PrefTag = Literal["quiet", "coffee", "kids", "museums", "parks", "no_bars", "warm", "night"]
PREF_LABELS_RU: dict[str, str] = {
    "quiet": "тихо",
    "coffee": "кофе",
    "kids": "с ребёнком",
    "museums": "музеи",
    "parks": "парки",
    "no_bars": "без баров",
    "warm": "в тепле",
    "night": "ночные",
}
MAX_PREFS = 8
MAX_EXCLUDE = 9  # «Ещё»: places already shown in this series (3 answers × 3 cards)
PoiId = Annotated[str, Field(pattern=r"^[a-z_]+:[A-Za-z0-9_./-]+$", max_length=200)]  # as POI.id


class GeoPoint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)


# --------------------------------------------------------------------------- data


class POI(BaseModel):
    """One point of interest. One JSON object per line in data/processed/poi.jsonl."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[a-z_]+:[A-Za-z0-9_./-]+$")  # e.g. "osm:node/123456"
    name: str = Field(min_length=1)
    name_i18n: dict[str, str] = Field(default_factory=dict)  # keys: ru / kk / en
    category: Category
    tags: list[str] = Field(default_factory=list)  # subset of TAG_VOCAB
    geo: GeoPoint
    h3_r9: str | None = None
    description: str | None = None
    description_source: Source | None = None  # honesty label for the description text
    address: str | None = None
    opening_hours: str | None = None  # raw OSM opening_hours string
    phone: str | None = None
    website: str | None = None
    wheelchair: Wheelchair = "unknown"
    kids_friendly: bool | None = None
    indoor: bool | None = None
    rating: float | None = Field(default=None, ge=0, le=5)
    rating_count: int | None = Field(default=None, ge=0)
    popularity: float = Field(default=0.0, ge=0, le=1)  # documented proxy, not a real rating
    sources: list[Source] = Field(min_length=1)
    data_confidence: float = Field(ge=0, le=1)  # completeness / trust of the record
    fetched_at: str  # ISO date of extraction, e.g. "2026-10-18"


class Landmark(BaseModel):
    """Named anchor the user can pick instead of sharing GPS (data/landmarks.json)."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[a-z0-9_]+$")
    names: dict[str, str]  # ru / kk / en
    aliases: list[str] = Field(default_factory=list)  # lowercase variants for free-text matching
    geo: GeoPoint
    kind: Literal["landmark", "transport", "district"] = "landmark"
    verified: bool = False  # True once the DATA lane checked the point against OSM


class WeatherInfo(BaseModel):
    temp_c: float
    precipitation_mm: float = 0.0
    wind_ms: float | None = None
    is_day: bool | None = None
    summary: str  # short human text, e.g. "+4°C, ветер 9 м/с"
    outdoor_ok: bool
    source: Literal["open_meteo"] = "open_meteo"
    fetched_at: datetime


# ------------------------------------------------------------------------ request


class LocationInput(BaseModel):
    """Exactly one of h3_cell / landmark_id. Raw coordinates are never accepted."""

    model_config = ConfigDict(extra="forbid")

    h3_cell: str | None = Field(default=None, pattern=r"^[0-9a-f]{15}$")
    landmark_id: str | None = Field(default=None, pattern=r"^[a-z0-9_]+$", max_length=64)

    @model_validator(mode="after")
    def _exactly_one(self) -> LocationInput:
        if (self.h3_cell is None) == (self.landmark_id is None):
            raise ValueError("provide exactly one of h3_cell or landmark_id")
        if self.h3_cell is not None and (int(self.h3_cell, 16) >> 52) & 0xF > MAX_H3_RES:
            # resolution bits of the H3 index; a finer cell is effectively a raw coordinate
            raise ValueError(f"H3 cell finer than r{MAX_H3_RES} is not accepted")
        return self


class UserOverrides(BaseModel):
    """Explicit UI filters. They win over whatever the query parser inferred."""

    model_config = ConfigDict(extra="forbid")

    time_budget_min: int | None = Field(default=None, ge=10, le=600)
    max_walk_min: int | None = Field(default=None, ge=1, le=120)
    with_kids: bool | None = None
    wheelchair: bool | None = None
    indoor_only: bool | None = None
    group_size: int | None = Field(default=None, ge=1, le=20)
    categories: list[Category] | None = Field(default=None, max_length=10)
    tourist: bool | None = None
    departure_in_min: int | None = Field(default=None, ge=30, le=1440)
    night: bool | None = None


class UserPrefs(BaseModel):
    """1.3.0. Likes and avoids for ONE request, only from PrefTag (labels: PREF_LABELS_RU).

    No free text and no history: the server does not store prefs and never logs them together
    with the cell, the landmark or the query. "no_bars" is a like ("без баров"), not an avoid.
    """

    model_config = ConfigDict(extra="forbid")

    likes: list[PrefTag] = Field(default_factory=list, max_length=MAX_PREFS)
    avoids: list[PrefTag] = Field(default_factory=list, max_length=MAX_PREFS)

    @model_validator(mode="after")
    def _consistent(self) -> UserPrefs:
        for name, tags in (("likes", self.likes), ("avoids", self.avoids)):
            if len(set(tags)) != len(tags):
                raise ValueError(f"duplicate tags in {name}")
        if set(self.likes) & set(self.avoids):
            raise ValueError("a tag cannot be in both likes and avoids")
        if "no_bars" in self.avoids:
            raise ValueError("no_bars is a like")
        return self


class RecommendRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=500)
    location: LocationInput
    at: datetime | None = None  # local-time override for demos; default = now in Astana (UTC+5)
    lang: Lang | None = None  # None = detect from the query
    overrides: UserOverrides | None = None
    want_route: bool = True
    use_llm: bool = True  # False = deterministic rules parser + template cards
    # 1.3.0, optional (1.2.0 requests stay valid):
    route_profile: RouteProfile = "fastest"
    prefs: UserPrefs | None = Field(default=None, repr=False)  # repr=False: not in logged reprs
    # «Ещё»: do not show these places again (poi_id of cards shown in this series, events too).
    # Per request only, never stored; not logged together with the cell or the query.
    exclude_poi_ids: list[PoiId] = Field(default_factory=list, max_length=MAX_EXCLUDE, repr=False)

    @model_validator(mode="after")
    def _unique_excludes(self) -> RecommendRequest:
        if len(set(self.exclude_poi_ids)) != len(self.exclude_poi_ids):
            raise ValueError("duplicate ids in exclude_poi_ids")
        return self


class QueryIntent(BaseModel):
    """What the user wants, produced by app/query_parser.py (LLM or rules)."""

    raw_query: str
    lang: Lang = "ru"
    time_budget_min: int | None = Field(default=None, ge=5, le=600)
    max_walk_min: int | None = Field(default=None, ge=1, le=120)
    categories: list[Category] = Field(default_factory=list)
    must_tags: list[str] = Field(default_factory=list)  # subset of TAG_VOCAB
    avoid_tags: list[str] = Field(default_factory=list)
    with_kids: bool = False
    wheelchair: bool = False
    group_size: int | None = Field(default=None, ge=1, le=20)
    indoor: Literal["indoor", "outdoor", "any"] = "any"
    night: bool = False
    tourist: bool = False
    landmark_mention: str | None = None  # landmark id matched in the text, if any
    parser: Literal["llm", "rules"] = "rules"


# ----------------------------------------------------------------- internal flow


class SearchContext(BaseModel):
    origin: GeoPoint  # centre of the H3 cell or the landmark, never the raw user point
    h3_cell: str
    h3_res: int
    area_label: str  # e.g. "у EXPO", "ваш район (≈1,8 га)"
    landmark_id: str | None = None
    local_time: datetime  # tz-aware, UTC+5
    weather: WeatherInfo | None = None
    radius_m: int = Field(ge=100, le=5000)


class HoursVerdict(BaseModel):
    status: OpenStatus
    confidence: Confidence
    note: str  # human text: "по данным OSM: ежедневно 10:00–22:00" / "нет данных о часах работы"
    closes_in_min: int | None = None
    opens_in_min: int | None = None


class Candidate(BaseModel):
    poi: POI
    distance_m: int = Field(ge=0)
    walk_min: int = Field(ge=0)
    hours: HoursVerdict
    score: float
    score_parts: dict[str, float] = Field(default_factory=dict)  # explainability / debug
    matched: list[str] = Field(default_factory=list)  # deterministic reasons, e.g. "есть кофе"


class CardDraft(BaseModel):
    """LLM output for one recommendation. Facts are NOT taken from here."""

    poi_id: str
    why: str
    what_to_do: str
    visit_min_low: int = Field(ge=5, le=300)
    visit_min_high: int = Field(ge=5, le=360)
    idea_source: Literal["poi_data", "generated"]


class DraftResponse(BaseModel):
    """Whole structured output of the generation step (app/generate.py)."""

    summary: str
    cards: list[CardDraft] = Field(default_factory=list, max_length=3)


# ------------------------------------------------------------------------ answer


class Card(BaseModel):
    """One recommendation. 1.3.0: kind="event" is a dated event at a venue (data/events.json):
    starts_at (tz-aware) and source_url are required for it and forbidden for a place."""

    rank: int = Field(ge=1, le=3)
    poi_id: str
    title: str
    category: Category
    geo: GeoPoint
    distance_m: int = Field(ge=0)  # computed by code
    walk_min: int = Field(ge=0)  # computed by code
    visit_min_low: int = Field(ge=5, le=300)
    visit_min_high: int = Field(ge=5, le=360)
    why: str
    what_to_do: str
    open_status: OpenStatus
    open_confidence: Confidence
    open_note: str
    idea_source: Literal["poi_data", "generated"]  # honesty label for the recommendation text
    facts_sources: list[Source] = Field(min_length=1)  # provenance of the facts on the card
    data_confidence: float = Field(ge=0, le=1)
    tags: list[str] = Field(default_factory=list)
    address: str | None = None  # for an event: venue name and address, from data/events.json
    website: str | None = None
    # 1.3.0, optional (1.2.0 cards stay valid):
    kind: Literal["place", "event"] = "place"
    starts_at: datetime | None = None  # event only, tz-aware (UTC+5)
    ends_at: datetime | None = None  # event only, optional, after starts_at
    source_url: str | None = Field(default=None, max_length=2048, pattern=r"^https://\S+$")

    @model_validator(mode="after")
    def _event_fields(self) -> Card:
        if self.kind == "place":
            if (self.starts_at, self.ends_at, self.source_url) != (None, None, None):
                raise ValueError("starts_at, ends_at and source_url are for events only")
            return self
        if self.starts_at is None or self.source_url is None:
            raise ValueError("an event needs starts_at and source_url")
        for value in (self.starts_at, self.ends_at):
            if value is not None and value.utcoffset() is None:
                raise ValueError("event times must carry a time zone")
        if self.ends_at is not None and self.ends_at <= self.starts_at:
            raise ValueError("ends_at must be after starts_at")
        return self


class RouteLeg(BaseModel):
    from_id: str  # "origin" or a poi_id
    to_id: str
    distance_m: int = Field(ge=0)
    walk_min: int = Field(ge=0)


# traffic lights / zebra / no markings / 1.3.0: markings not known (OSM gives no crossing type)
CrossingKind = Literal["signals", "marked", "unmarked", "unknown"]


class RouteCrossing(BaseModel):
    """A street crossing on the walking route: public OSM infrastructure, not a user position."""

    model_config = ConfigDict(extra="forbid")

    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    kind: CrossingKind


class MiniRoute(BaseModel):
    stop_ids: list[str] = Field(min_length=1, max_length=3)
    legs: list[RouteLeg]  # distance and minutes along the walk graph when path_source="graph"
    total_walk_m: int = Field(ge=0)
    total_min: int = Field(ge=0)  # walking + visiting (low estimate)
    fits_budget: bool
    path: list[GeoPoint]  # origin -> stops: along the walk graph, or straight segments
    note: str  # e.g. "Маршрут приблизительный: прямые отрезки × 1,3"
    # 1.1.0, all optional with defaults (old answers stay valid):
    path_source: Literal["graph", "straight"] = "straight"
    crossings: list[RouteCrossing] = Field(default_factory=list, max_length=50)  # graph only
    steps_count: int = Field(default=0, ge=0)  # stair segments on the graph path
    # 1.3.0, all optional with defaults (old answers stay valid). Facts computed by code:
    profile: RouteProfile = "fastest"  # the profile the route was actually planned with
    lit_share: float | None = Field(default=None, ge=0, le=1)  # share of path length, OSM lit=yes
    unlit_share: float | None = Field(default=None, ge=0, le=1)  # OSM lit=no
    unknown_lit_share: float | None = Field(default=None, ge=0, le=1)  # no lit tag; straight = 1
    kerb_lowered: int | None = Field(default=None, ge=0)  # crossings with kerb=lowered/flush
    kerb_unknown: int | None = Field(default=None, ge=0)  # crossings without kerb data
    max_outdoor_leg_min: int | None = Field(default=None, ge=0)  # longest walk between stops

    @model_validator(mode="after")
    def _lit_shares(self) -> MiniRoute:
        shares = (self.lit_share, self.unlit_share, self.unknown_lit_share)
        if all(v is None for v in shares):
            return self
        if any(v is None for v in shares):
            raise ValueError("lit_share, unlit_share and unknown_lit_share go together")
        if abs(sum(v for v in shares if v is not None) - 1) > 0.02:
            raise ValueError("lit shares must sum to 1")
        return self


class DataUsed(BaseModel):
    """Transparency panel "Какие данные использованы"."""

    location: str
    time: str
    weather: str | None = None
    filters: list[str] = Field(default_factory=list)
    sources: list[str] = Field(default_factory=list)
    candidates_considered: int = Field(ge=0)
    parser: Literal["llm", "rules"]
    generation: Literal["llm", "template"]
    model: str | None = None


class Answer(BaseModel):
    schema_version: str = SCHEMA_VERSION
    request_id: str
    lang: Lang
    summary: str
    cards: list[Card] = Field(default_factory=list, max_length=3)
    route: MiniRoute | None = None
    intent: QueryIntent
    context: SearchContext
    data_used: DataUsed
    warnings: list[str] = Field(default_factory=list)
    disclaimer: str
    latency_ms: int = Field(default=0, ge=0)
