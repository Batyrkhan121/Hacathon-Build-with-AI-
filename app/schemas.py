"""Qadam — data contract between all layers.

Owner: INTEGRATOR (main branch only; wave 8: the wave-8 branch; wave 9: wave-9).
Lanes must NOT edit this file. Need a change? Put a "CONTRACT CHANGE REQUEST" into your lane
report; the integrator applies it on main (wave 8: on wave-8, wave 9: on wave-9) and tells
the lanes which commit to merge.

Flow: pipeline -> data/processed/poi.jsonl (POI) -> store/retrieval (Candidate)
      -> generate (DraftResponse) -> validate (Card) -> pipeline (Answer) -> api -> web/eval
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

SCHEMA_VERSION = "1.4.1"
# 1.1.0: walking route on the OSM walk graph; 1.2.0: cells up to r10;
# 1.3.0 (wave 8): route_profile, prefs and exclude_poi_ids in the request, route profile facts,
# event cards, crossing kind unknown; 1.3.1 (wave 8.1): separate likes / avoids vocabularies,
# conflicting pairs -> 422, MiniRoute.straight_share, Card.venue; 1.4.0 (wave 9): city gaps map
# (CityGaps in data/processed/city_gaps.json, GET /api/city-gaps); requests and answers unchanged;
# 1.4.1 (wave 9.1): districts in CityGaps (GapDistricts, GapCell.district)
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
# быстрее / без ступенек / ночью — по крупным улицам (lit: the API name kept for compatibility)
RouteProfile = Literal["fastest", "accessible", "lit"]

# Fixed preference vocabularies. No free text: an unknown tag is a 422.
# 1.3.1: likes and avoids have separate vocabularies («без баров» left likes: avoids "bars").
LikeTag = Literal["quiet", "coffee", "kids", "museums", "parks", "warm", "night"]
AvoidTag = Literal["bars", "noisy", "long_outdoor", "kids_places", "cafes"]
PrefTag = Literal[LikeTag, AvoidTag]  # any tag of either list
PREF_LIKE_LABELS_RU: dict[str, str] = {
    "quiet": "тихо",
    "coffee": "кофе",
    "kids": "с ребёнком",
    "museums": "музеи",
    "parks": "парки",
    "warm": "в тепле",
    "night": "ночные",
}
PREF_AVOID_LABELS_RU: dict[str, str] = {
    "bars": "бары",
    "noisy": "шумно",
    "long_outdoor": "долго на улице",
    "kids_places": "детские места",
    "cafes": "кафе",
}
PREF_LABELS_RU: dict[str, str] = {**PREF_LIKE_LABELS_RU, **PREF_AVOID_LABELS_RU}
# a like and an avoid that contradict each other: both in one request -> 422
# 1.3.0 like -> 1.3.1 avoid, read as a synonym for one version (until 1.4), never a 422
DEPRECATED_LIKES: dict[str, str] = {"no_bars": "bars"}
PREF_CONFLICTS: frozenset[tuple[str, str]] = frozenset(
    {("kids", "kids_places"), ("coffee", "cafes")}
)
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
    """Likes and avoids for ONE request. 1.3.1: likes only from LikeTag, avoids only from AvoidTag
    (labels: PREF_LIKE_LABELS_RU, PREF_AVOID_LABELS_RU); a pair from PREF_CONFLICTS is a 422.

    No free text and no history: the server does not store prefs and never logs them together
    with the cell, the landmark or the query.
    """

    model_config = ConfigDict(extra="forbid")

    likes: list[LikeTag] = Field(default_factory=list, max_length=MAX_PREFS)
    avoids: list[AvoidTag] = Field(default_factory=list, max_length=MAX_PREFS)

    @model_validator(mode="before")
    @classmethod
    def _deprecated_likes(cls, data: object) -> object:
        """«no_bars» in likes (1.3.0) moves to avoids as «bars»; anything malformed is left to
        the normal validation (a 422)."""
        if not isinstance(data, dict):
            return data
        likes, avoids = data.get("likes"), data.get("avoids", [])
        if not isinstance(likes, list) or not isinstance(avoids, list):
            return data
        moved = [DEPRECATED_LIKES[t] for t in likes if isinstance(t, str) and t in DEPRECATED_LIKES]
        if not moved:
            return data
        kept = [t for t in likes if not (isinstance(t, str) and t in DEPRECATED_LIKES)]
        added = [t for t in dict.fromkeys(moved) if t not in avoids]
        return {**data, "likes": kept, "avoids": avoids + added}

    @model_validator(mode="after")
    def _consistent(self) -> UserPrefs:
        for name, tags in (("likes", self.likes), ("avoids", self.avoids)):
            if len(set(tags)) != len(tags):
                raise ValueError(f"duplicate tags in {name}")
        likes, avoids = set(self.likes), set(self.avoids)
        if likes & avoids or any(a in likes and b in avoids for a, b in PREF_CONFLICTS):
            raise ValueError("a like and an avoid contradict each other")
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
    venue: str | None = Field(default=None, max_length=200)  # 1.3.1, event only: the venue name

    @model_validator(mode="after")
    def _event_fields(self) -> Card:
        if self.kind == "place":
            if (self.starts_at, self.ends_at, self.source_url, self.venue) != (None,) * 4:
                raise ValueError("starts_at, ends_at, source_url and venue are for events only")
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
    # 1.3.1: share of the path length on straight segments (0 = all on the walk graph,
    # 1 = all straight); «без ступенек» is claimed only at 0
    straight_share: float | None = Field(default=None, ge=0, le=1)

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


# ------------------------------------------------------------- city gaps map (1.4.0)

GAP_H3_RES = 8  # cells of the city gaps map: r8 ≈ 0,74 км², a district view; aggregates only
GAP_MAX_CELLS = 5000  # Astana with its outskirts is ≈ 1 500 r8 cells
# readers show a share only from these totals up; below them the cell is «мало данных»
GAP_MIN_POI = 5
GAP_MIN_WALK_M = 300
GAP_MIN_CROSSINGS = 3
OSM_ATTRIBUTION = "© OpenStreetMap contributors"
ODBL_URL = "https://opendatacommons.org/licenses/odbl/1-0/"
OSM_COPYRIGHT_URL = "https://www.openstreetmap.org/copyright"
IsoDate = Annotated[str, Field(pattern=r"^\d{4}-\d{2}-\d{2}$")]
# 1.4.1: districts (Megah, 22.10)
DistrictStatus = Literal["ok", "stale", "missing"]
GAP_MAX_DISTRICTS = 20
GAP_MAX_OUTLINE_POINTS = 4000  # per district, all rings together (simplified OSM boundary)


class GapCounts(BaseModel):
    """Open-data completeness counters of one area (an r8 cell or the whole city).

    Totals and their «no data» parts only; readers compute shares (part / total). Exact
    definitions: docs/ARCHITECTURE.md §5, «Контракт 1.4.0: карта пробелов данных».
    """

    model_config = ConfigDict(extra="forbid")

    poi: int = Field(ge=0)  # places of data/processed/poi.jsonl in the area (by POI.geo)
    poi_no_hours: int = Field(ge=0)  # opening_hours empty or missing
    poi_wheelchair_unknown: int = Field(ge=0)  # wheelchair == "unknown"
    poi_no_name_kk: int = Field(ge=0)  # no Kazakh name in name_i18n["kk"]
    walk_m: int = Field(ge=0)  # walk graph edge length, metres (edge counted by its midpoint)
    walk_lit_unknown_m: int = Field(ge=0)  # ... with neither lit (edge bit 2) nor unlit (bit 32)
    crossings: int = Field(ge=0)  # crossing nodes (node bit 1)
    crossings_kind_unknown: int = Field(ge=0)  # none of node bits 2, 8, 32: kind unknown
    crossings_kerb_unknown: int = Field(ge=0)  # neither kerb lowered (bit 4) nor raised (bit 16)

    @model_validator(mode="after")
    def _parts_within_totals(self) -> GapCounts:
        pairs = ((self.poi_no_hours, self.poi), (self.poi_wheelchair_unknown, self.poi),
                 (self.poi_no_name_kk, self.poi), (self.walk_lit_unknown_m, self.walk_m),
                 (self.crossings_kind_unknown, self.crossings),
                 (self.crossings_kerb_unknown, self.crossings))
        if any(part > total for part, total in pairs):
            raise ValueError("a «no data» count exceeds its total")
        return self


class GapCell(GapCounts):
    """One r8 cell of the map; cells with no places, no walk graph and no crossings are left out."""

    h3_cell: str = Field(pattern=r"^[0-9a-f]{15}$")
    # 1.4.1: the district whose outline holds the cell centre; None = in no district outline
    district: str | None = Field(default=None, pattern=r"^[a-z0-9_]+$", max_length=40)

    @model_validator(mode="after")
    def _r8_cell(self) -> GapCell:
        index = int(self.h3_cell, 16)
        if (index >> 59) & 0xF != 1 or (index >> 52) & 0xF != GAP_H3_RES:
            # mode bits (1 = cell) and resolution bits of the H3 index
            raise ValueError(f"h3_cell must be an H3 cell of resolution {GAP_H3_RES}")
        if self.poi == 0 and self.walk_m == 0 and self.crossings == 0:
            raise ValueError("empty cells are left out of the map")
        return self


LatLon = tuple[Annotated[float, Field(ge=-90, le=90)], Annotated[float, Field(ge=-180, le=180)]]


def _sum_counts(cells: list[GapCell]) -> dict[str, int]:
    return {name: sum(getattr(cell, name) for cell in cells) for name in GapCounts.model_fields}


class GapDistrict(BaseModel):
    """1.4.1: one district of the official list. `status`: ok — the OSM boundary matches the list;
    stale — an OSM boundary exists but is outdated (counted by it, and the map says so); missing —
    no OSM boundary: no outline and no counts, its area falls to `GapDistricts.unassigned`."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[a-z0-9_]+$", max_length=40)
    name: dict[str, str]  # ru / kk / en, as on the official list
    status: DistrictStatus
    osm_relation: int | None = Field(default=None, gt=0)  # the OSM boundary relation
    boundary_date: IsoDate | None = None  # last change of that boundary in OSM
    cell_count: int = Field(ge=0)  # cells whose centre is inside the outline
    counts: GapCounts | None = None  # exactly the sum over those cells
    outline: list[list[LatLon]] = Field(default_factory=list)  # closed rings of (lat, lon)

    @model_validator(mode="after")
    def _status_fields(self) -> GapDistrict:
        if set(self.name) != {"ru", "kk", "en"} or not all(v.strip() for v in self.name.values()):
            raise ValueError("a district needs a name in ru, kk and en")
        has_boundary = self.status != "missing"
        parts = (self.osm_relation is not None, self.boundary_date is not None,
                 self.counts is not None, bool(self.outline))
        if any(part != has_boundary for part in parts):
            raise ValueError("a missing district has no boundary, date, counts or outline; "
                             "ok and stale districts have all of them")
        if not has_boundary and self.cell_count:
            raise ValueError("a missing district holds no cells")
        if any(len(ring) < 4 or ring[0] != ring[-1] for ring in self.outline):
            raise ValueError("outline rings are closed and have at least 4 points")
        if sum(len(ring) for ring in self.outline) > GAP_MAX_OUTLINE_POINTS:
            raise ValueError(f"an outline has at most {GAP_MAX_OUTLINE_POINTS} points")
        return self


class GapDistricts(BaseModel):
    """1.4.1: the districts block of CityGaps. Districts plus `unassigned` add up to `city`."""

    model_config = ConfigDict(extra="forbid")

    boundaries_source: Literal["osm"] = "osm"  # OSM administrative boundaries, ODbL as the rest
    boundaries_date: IsoDate  # OSM data date of the boundaries used
    # the akimat page the district list was checked against, and when; None, None: not checked
    official_list_url: str | None = Field(default=None, pattern=r"^https://", max_length=300)
    official_list_checked: IsoDate | None = None
    items: list[GapDistrict] = Field(min_length=1, max_length=GAP_MAX_DISTRICTS)
    unassigned: GapCounts  # cells whose centre is in no outline: exactly their sum
    unassigned_cells: int = Field(ge=0)

    @model_validator(mode="after")
    def _list_check(self) -> GapDistricts:
        if (self.official_list_url is None) != (self.official_list_checked is None):
            raise ValueError("official_list_url and official_list_checked go together")
        ids = [d.id for d in self.items]
        if len(ids) != len(set(ids)):
            raise ValueError("district ids are unique")
        return self


class CityGaps(BaseModel):
    """«Карта пробелов данных»: data/processed/city_gaps.json, served as is by GET /api/city-gaps.

    Aggregates of open data only (OSM places and the OSM walk graph): no user data, no requests,
    no per-place lists. Built by the pipeline (DATA); the server validates it on load and never
    recomputes it. Shares and the «мало данных» thresholds (GAP_MIN_*) are the readers' job.
    """

    model_config = ConfigDict(extra="forbid")

    format_version: Literal[1] = 1
    built_at: datetime  # when the pipeline built the file, with a time zone
    poi_date: IsoDate  # newest POI.fetched_at
    walk_graph_date: IsoDate  # newest OSM snapshot of the walk graph (walk_graph_meta.json)
    h3_res: Literal[8] = 8  # = GAP_H3_RES
    attribution: Literal["© OpenStreetMap contributors"] = "© OpenStreetMap contributors"
    license: Literal["ODbL-1.0"] = "ODbL-1.0"
    license_url: Literal["https://opendatacommons.org/licenses/odbl/1-0/"] = (
        "https://opendatacommons.org/licenses/odbl/1-0/")
    copyright_url: Literal["https://www.openstreetmap.org/copyright"] = (
        "https://www.openstreetmap.org/copyright")
    city: GapCounts  # the whole city: exactly the sum over cells
    cells: list[GapCell] = Field(max_length=GAP_MAX_CELLS)
    districts: GapDistricts | None = None  # 1.4.1; None: no district data (as in 1.4.0)

    @model_validator(mode="after")
    def _consistent(self) -> CityGaps:
        if self.built_at.tzinfo is None:
            raise ValueError("built_at must carry a time zone")
        ids = [cell.h3_cell for cell in self.cells]
        if ids != sorted(set(ids)):
            raise ValueError("cells must be unique and sorted by h3_cell")
        if self.city.model_dump() != _sum_counts(self.cells):
            raise ValueError("city is not the sum over cells")
        if self.districts is None:
            if any(cell.district is not None for cell in self.cells):
                raise ValueError("cells name districts, but the file has no districts block")
            return self
        # districts + unassigned add up to the city: each district is exactly the sum of its cells
        by_id = {d.id: d for d in self.districts.items}
        for cell in self.cells:
            if cell.district is not None and (cell.district not in by_id
                                              or by_id[cell.district].status == "missing"):
                raise ValueError(f"cell {cell.h3_cell}: no outline for district {cell.district!r}")
        for d in self.districts.items:
            mine = [cell for cell in self.cells if cell.district == d.id]
            if d.cell_count != len(mine) or (d.counts is not None
                                             and d.counts.model_dump() != _sum_counts(mine)):
                raise ValueError(f"district {d.id}: counts are not the sum over its cells")
        rest = [cell for cell in self.cells if cell.district is None]
        if (self.districts.unassigned_cells != len(rest)
                or self.districts.unassigned.model_dump() != _sum_counts(rest)):
            raise ValueError("unassigned is not the sum over cells outside the districts")
        return self
