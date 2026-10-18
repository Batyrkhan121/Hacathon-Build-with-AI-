"""T23: mini-route — nearest-neighbour order, legs, budget, honest note."""

from __future__ import annotations

from app.geo import haversine_m, walk_minutes
from app.route import plan_route, plan_walk, route_note, walk_visit_minutes
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


# ------------------------------------------------------------ wave 3 (T71)


def placed(poi_id: str, dlon: float, *, category: str = "park", name: str | None = None,
           score: float = 0.5) -> Candidate:
    base = stop(poi_id, 0.0, dlon)
    poi = base.poi.model_copy(update={"category": category, "name": name or base.poi.name})
    return base.model_copy(update={"poi": poi, "score": score})


def test_exact_order_beats_nearest_neighbour() -> None:
    """East 1, west 1.2, east 2.5 (units of 0.004°): NN goes E1→E2.5→W1.2, exact W→E1→E2.5."""
    e1, w, e2 = placed("e1", 0.004), placed("w", -0.0048), placed("e2", 0.010)
    route = plan_route(ORIGIN, [e1, w, e2], None, {})
    assert route is not None
    assert route.stop_ids == ["test:poi/w", "test:poi/e1", "test:poi/e2"]


def test_loop_returns_to_start() -> None:
    one_way = plan_route(ORIGIN, [NEAR, MID], None, VISITS)
    loop = plan_route(ORIGIN, [NEAR, MID], None, VISITS, loop=True)
    assert one_way is not None and loop is not None
    assert loop.legs[-1].to_id == "origin" and loop.legs[-1].from_id == loop.stop_ids[-1]
    assert loop.path[0] == ORIGIN and loop.path[-1] == ORIGIN
    assert len(loop.legs) == len(loop.stop_ids) + 1
    assert loop.total_min == one_way.total_min + loop.legs[-1].walk_min
    assert "с возвращением к старту" in loop.note and "без обратной дороги" in one_way.note


def test_loop_budget_counts_the_way_back() -> None:
    loop = plan_route(ORIGIN, [FAR], None, VISITS, loop=True)
    assert loop is not None
    tight = plan_route(ORIGIN, [FAR, NEAR], loop.total_min, VISITS, loop=True)
    assert tight is not None and tight.stop_ids == ["test:poi/far"] and tight.fits_budget


def test_route_note_languages_and_modes() -> None:
    assert route_note("en", loop=True).startswith("Approximate route: straight segments × 1.3")
    assert "back to the start" in route_note("en", loop=True)
    assert "кері жолсыз" in route_note("kk")
    assert route_note("ru", kids=True).endswith("С ребёнком закладывайте больше времени.")
    wheel = route_note("ru", kids=True, wheelchair=True)
    assert "закладывайте больше времени" in wheel and "ступени" in wheel
    assert "allow extra time" in route_note("en", wheelchair=True)
    note = "Свой текст"
    route = plan_route(ORIGIN, [NEAR], None, {}, note=note)
    assert route is not None and route.note == note


def test_plan_walk_picks_three_that_fit() -> None:
    cands = [placed("a", 0.002, score=0.9), placed("b", 0.004, category="monument", score=0.8),
             placed("c", -0.003, category="cafe", score=0.7), placed("d", 0.03, score=0.95)]
    picked = plan_walk(ORIGIN, cands, 90)
    ids = {c.poi.id for c in picked}
    assert len(picked) == 3 and "test:poi/d" not in ids  # d is too far for 90 min round trip
    route = plan_route(ORIGIN, picked, 90, {c.poi.id: walk_visit_minutes(c) for c in picked},
                       loop=True)
    assert route is not None and route.fits_budget and len(route.stop_ids) == 3


def test_plan_walk_skips_fast_food_and_bars_unless_asked() -> None:
    cands = [placed("burger", 0.001, category="fast_food", score=0.99),
             placed("pub", 0.0015, category="bar", score=0.98),
             placed("park", 0.002, score=0.5), placed("monument", 0.003, category="monument")]
    picked = {c.poi.category for c in plan_walk(ORIGIN, cands, 90)}
    assert picked and not picked & {"fast_food", "bar"}
    asked = {c.poi.category for c in plan_walk(ORIGIN, cands, 90,
                                               wanted_categories={"fast_food"})}
    assert "fast_food" in asked and "bar" not in asked


def test_plan_walk_respects_budget_and_diversity() -> None:
    same = [placed(f"p{i}", 0.001 * (i + 1), name="Детская площадка", category="playground")
            for i in range(3)]
    cafes = [placed(f"c{i}", -0.001 * (i + 1), category="cafe") for i in range(3)]
    picked = plan_walk(ORIGIN, same + cafes, 120)
    names = [c.poi.name for c in picked]
    assert names.count("Детская площадка") <= 1
    assert sum(c.poi.category == "cafe" for c in picked) <= 2
    assert plan_walk(ORIGIN, [placed("far", 0.05)], 30) == []  # nothing fits
    small = plan_walk(ORIGIN, [placed("a", 0.001), placed("b", 0.0012, category="cafe")], 25)
    assert len(small) == 1  # 20-min park + way there and back fits, two stops do not
