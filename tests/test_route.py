"""T23: mini-route — nearest-neighbour order, legs, budget, honest note."""

from __future__ import annotations

from app.geo import haversine_m, walk_minutes
from app.route import plan_route
from app.schemas import POI, Candidate, GeoPoint, HoursVerdict

from .conftest import EXPO

ORIGIN = GeoPoint(lat=EXPO[0], lon=EXPO[1])
UNKNOWN = HoursVerdict(status="unknown", confidence="low", note="нет данных о часах работы")


def stop(poi_id: str, dlat: float, dlon: float = 0.0) -> Candidate:
    poi = POI.model_validate(
        {
            "id": f"test:poi/{poi_id}",
            "name": f"Место {poi_id}",
            "category": "park",
            "geo": {"lat": EXPO[0] + dlat, "lon": EXPO[1] + dlon},
            "sources": ["osm"],
            "data_confidence": 0.5,
            "fetched_at": "2026-10-18",
        }
    )
    distance = haversine_m(ORIGIN, poi.geo)
    return Candidate(
        poi=poi,
        distance_m=round(distance),
        walk_min=walk_minutes(distance),
        hours=UNKNOWN,
        score=0.5,
    )


FAR, NEAR, MID = stop("far", 0.010), stop("near", 0.002), stop("mid", 0.005)
VISITS = {"test:poi/far": 20, "test:poi/near": 15, "test:poi/mid": 10}


def test_no_stops() -> None:
    assert plan_route(ORIGIN, [], 60, {}) is None


def test_nearest_neighbour_order() -> None:
    route = plan_route(ORIGIN, [FAR, NEAR, MID], None, VISITS)
    assert route is not None
    assert route.stop_ids == ["test:poi/near", "test:poi/mid", "test:poi/far"]
    assert [leg.from_id for leg in route.legs] == ["origin", "test:poi/near", "test:poi/mid"]
    assert [leg.to_id for leg in route.legs] == route.stop_ids
    assert route.path[0] == ORIGIN
    assert route.path[1:] == [NEAR.poi.geo, MID.poi.geo, FAR.poi.geo]


def test_legs_and_totals() -> None:
    route = plan_route(ORIGIN, [FAR, NEAR, MID], None, VISITS)
    assert route is not None
    first = route.legs[0]
    assert first.distance_m == round(haversine_m(ORIGIN, NEAR.poi.geo))
    assert first.walk_min == walk_minutes(haversine_m(ORIGIN, NEAR.poi.geo))
    assert route.total_walk_m == sum(leg.distance_m for leg in route.legs)
    assert route.total_min == sum(leg.walk_min for leg in route.legs) + 45
    assert route.fits_budget is True


def test_budget_drops_lowest_ranked_stop() -> None:
    # cards come ranked: FAR is the best card, MID the weakest
    full = plan_route(ORIGIN, [FAR, NEAR, MID], None, VISITS)
    assert full is not None
    without_mid = plan_route(ORIGIN, [FAR, NEAR], None, VISITS)
    assert without_mid is not None
    route = plan_route(ORIGIN, [FAR, NEAR, MID], without_mid.total_min, VISITS)
    assert route is not None
    assert route.stop_ids == ["test:poi/near", "test:poi/far"]  # re-ordered after the drop
    assert route.total_min == without_mid.total_min
    assert route.fits_budget is True
    assert len(route.path) == 3


def test_max_stops_never_exceeds_three() -> None:
    extra = [stop(f"x{i}", 0.001 * i, 0.001) for i in range(1, 4)]
    route = plan_route(ORIGIN, [FAR, NEAR, MID, *extra], None, VISITS, max_stops=6)
    assert route is not None
    assert len(route.stop_ids) == 3


def test_single_stop_over_budget_is_kept_but_flagged() -> None:
    route = plan_route(ORIGIN, [FAR], 5, VISITS)
    assert route is not None
    assert route.stop_ids == ["test:poi/far"]
    assert route.fits_budget is False


def test_max_stops_and_duplicates() -> None:
    extra = stop("extra", 0.001)
    route = plan_route(ORIGIN, [FAR, NEAR, NEAR, MID, extra], None, VISITS, max_stops=3)
    assert route is not None
    assert sorted(route.stop_ids) == ["test:poi/far", "test:poi/mid", "test:poi/near"]
    assert plan_route(ORIGIN, [FAR], None, VISITS, max_stops=0) is None


def test_default_visit_minutes() -> None:
    route = plan_route(ORIGIN, [NEAR], None, {})
    assert route is not None
    assert route.total_min == route.legs[0].walk_min + 20


def test_honest_note() -> None:
    route = plan_route(ORIGIN, [NEAR, MID], None, VISITS)
    assert route is not None
    assert "прямые отрезки × 1,3" in route.note
