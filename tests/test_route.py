"""T23: mini-route — nearest-neighbour order, legs, budget, honest note."""

from __future__ import annotations

import pytest

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


def test_plan_walk_prefers_a_walk_stop_over_more_stops() -> None:
    """P1 (Mega Silk Way, 60 min): «mall at 0 m + coffee 87 m» must not beat the park."""
    mall = placed("mall", 0.0, category="mall", score=0.38)
    coffee = placed("coffee", 0.00125, category="cafe", score=0.37)
    park = placed("park", 0.0085, score=0.75)
    garden = placed("garden", 0.0093, score=0.74)
    picked = plan_walk(ORIGIN, [park, garden, mall, coffee], 60)
    ids = [c.poi.id for c in picked]
    assert ids and ids[0] in {"test:poi/park", "test:poi/garden"}
    assert "test:poi/mall" not in ids


def test_plan_walk_never_stops_where_you_already_stand() -> None:
    here = placed("here", 0.0, category="monument", score=0.99)
    across = placed("across", 0.0013, category="cafe", score=0.98)  # ≈90 m: still "here"
    assert here.distance_m < 100 and across.distance_m < 100
    picked = plan_walk(ORIGIN, [here, across, placed("near", 0.003, score=0.4)], 60)
    assert [c.poi.id for c in picked] == ["test:poi/near"]


def test_plan_walk_anchor_best_ranked_place_that_fits_alone() -> None:
    """The best-ranked stop (A, 600 m) fits alone -> it is in the walk, even though the pair
    B+C on the other side has a higher total score and also fits."""
    a = placed("a", 0.0085, score=0.9)
    b = placed("b", -0.0043, score=0.8)
    c = placed("c", -0.0050, category="monument", score=0.7)
    picked = plan_walk(ORIGIN, [a, b, c], 60)
    assert "test:poi/a" in {x.poi.id for x in picked}


def test_plan_walk_anchor_then_more_stops_then_score() -> None:
    a = placed("a", 0.0043, score=0.9)
    near_a = [placed("x1", 0.0050, category="monument", score=0.3),
              placed("x2", 0.0057, category="cafe", score=0.2),
              placed("x3", 0.0050 + 0.0001, category="viewpoint", score=0.6)]
    picked = plan_walk(ORIGIN, [a, *near_a], 90)
    ids = {x.poi.id for x in picked}
    assert "test:poi/a" in ids and len(picked) == 3  # more stops first
    assert "test:poi/x3" in ids  # then the higher total score


def test_plan_walk_without_a_fitting_anchor_falls_back() -> None:
    far = placed("far", 0.03, score=0.95)  # does not fit alone in 40 min
    picked = plan_walk(ORIGIN, [far, placed("p", 0.003, score=0.5)], 40)
    assert [x.poi.id for x in picked] == ["test:poi/p"]


def test_plan_walk_score_beats_stop_count_among_walk_stops() -> None:
    best = placed("best", 0.006, score=0.9)
    weak = [placed(f"w{i}", 0.001 * (i + 2), category="monument", score=0.2) for i in range(3)]
    picked = plan_walk(ORIGIN, [best, *weak], 40)
    assert "test:poi/best" in {c.poi.id for c in picked}


def test_plan_walk_without_walk_stops_still_works() -> None:
    cafes = [placed(f"c{i}", 0.002 * (i + 1), category="cafe", score=0.5) for i in range(2)]
    assert len(plan_walk(ORIGIN, cafes, 90)) == 2


def test_plan_walk_skips_fast_food_and_bars_unless_asked() -> None:
    cands = [placed("burger", 0.0016, category="fast_food", score=0.99),
             placed("pub", 0.0018, category="bar", score=0.98),
             placed("park", 0.002, score=0.5), placed("monument", 0.003, category="monument")]
    picked = {c.poi.category for c in plan_walk(ORIGIN, cands, 90)}
    assert picked and not picked & {"fast_food", "bar"}
    asked = {c.poi.category for c in plan_walk(ORIGIN, cands, 90,
                                               wanted_categories={"fast_food"})}
    assert "fast_food" in asked and "bar" not in asked
    dinner = placed("dinner", 0.0025, category="restaurant", score=0.99)
    assert "restaurant" not in {c.poi.category for c in plan_walk(ORIGIN, [dinner, *cands], 90)}


def test_plan_walk_respects_budget_and_diversity() -> None:
    same = [placed(f"p{i}", 0.0015 * (i + 1), name="Детская площадка", category="playground")
            for i in range(3)]
    cafes = [placed(f"c{i}", -0.0015 * (i + 1), category="cafe") for i in range(3)]
    picked = plan_walk(ORIGIN, same + cafes, 120)
    names = [c.poi.name for c in picked]
    assert names.count("Детская площадка") <= 1
    assert sum(c.poi.category == "cafe" for c in picked) <= 2
    assert plan_walk(ORIGIN, [placed("far", 0.05)], 30) == []  # nothing fits
    small = plan_walk(ORIGIN, [placed("a", 0.0015), placed("b", 0.0017, category="cafe")], 25)
    assert len(small) == 1  # 20-min park + way there and back fits, two stops do not


# ------------------------------------------------ T91: route on the walk graph

from app.walkgraph import WALK, Mode, load_graph  # noqa: E402

from .test_walkgraph import STREET, STREET_EDGES, at, write_graph  # noqa: E402


def cand_at(poi_id: str, point: GeoPoint, category: str = "park", score: float = 0.5) -> Candidate:
    poi = POI.model_validate({
        "id": f"test:poi/{poi_id}", "name": f"Место {poi_id}", "category": category,
        "geo": {"lat": point.lat, "lon": point.lon}, "sources": ["osm"],
        "data_confidence": 0.5, "fetched_at": "2026-10-19",
    })
    d = haversine_m(at(0, -5), point)
    return Candidate(poi=poi, distance_m=round(d), walk_min=walk_minutes(d), hours=UNKNOWN,
                     score=score)


def test_route_on_graph_follows_footways_and_crossings(tmp_path) -> None:  # type: ignore[no-untyped-def]
    g = load_graph(write_graph(tmp_path, STREET, STREET_EDGES))
    east, north = cand_at("east", at(200, -5)), cand_at("north", at(0, 35))
    r = plan_route(at(0, -5), [east, north], None, {}, graph=g)
    assert r is not None
    assert r.path_source == "graph"
    assert sum(leg.distance_m for leg in r.legs) == r.total_walk_m
    assert all(leg.walk_min == max(1, -(-leg.distance_m // 75)) for leg in r.legs)
    assert r.total_walk_m >= 430  # across the road only at the crossing (x = 200)
    assert [c.kind for c in r.crossings] == ["signals"]
    assert r.steps_count == 0
    assert (r.path[0].lat, r.path[0].lon) == (at(0, -5).lat, at(0, -5).lon)
    last = {"test:poi/east": east, "test:poi/north": north}[r.stop_ids[-1]].poi.geo
    assert (r.path[-1].lat, r.path[-1].lon) == (last.lat, last.lon)
    assert "OSM" in r.note and "прямые отрезки" not in r.note


def test_route_without_binding_stays_straight(tmp_path) -> None:  # type: ignore[no-untyped-def]
    g = load_graph(write_graph(tmp_path, STREET, STREET_EDGES))
    far = cand_at("far", at(0, -900))  # 900 m from any node: cannot be bound
    near = cand_at("near", at(200, -5))
    r = plan_route(at(0, -5), [near, far], None, {}, graph=g)
    assert r is not None
    assert r.path_source == "straight"
    assert r.crossings == [] and r.steps_count == 0
    assert "прямые отрезки" in r.note


def test_wheelchair_route_avoids_stairs(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from .test_walkgraph import FOOTWAY, STEPS

    nodes = {"A": (0, 0, 0), "M": (0, 20, 0), "B": (0, 40, 0), "R1": (30, 0, 0),
             "R2": (30, 40, 0), "C": (0, 60, 0)}
    edges = [("A", "M", STEPS), ("M", "B", STEPS), ("A", "R1", FOOTWAY),
             ("R1", "R2", FOOTWAY), ("R2", "B", FOOTWAY), ("B", "C", FOOTWAY)]
    g = load_graph(write_graph(tmp_path, nodes, edges))
    stops = [cand_at("b", at(0, 40)), cand_at("c", at(0, 60))]
    walk = plan_route(at(0, 0), stops, None, {}, graph=g)
    chair = plan_route(at(0, 0), stops, None, {}, graph=g, mode=Mode(wheelchair=True))
    assert walk is not None and chair is not None
    assert walk.steps_count == 2 and chair.steps_count == 0
    assert chair.total_walk_m > walk.total_walk_m


def test_budget_is_checked_with_graph_minutes(tmp_path) -> None:  # type: ignore[no-untyped-def]
    g = load_graph(write_graph(tmp_path, STREET, STREET_EDGES))
    east, north = cand_at("east", at(200, -5)), cand_at("north", at(0, 35))
    visits = {"test:poi/east": 10, "test:poi/north": 10}
    straight = plan_route(at(0, -5), [east, north], 27, visits)
    assert straight is not None and len(straight.stop_ids) == 2  # straight: 25 of 27 min
    graph = plan_route(at(0, -5), [east, north], 27, visits, graph=g)  # graph: 28 min
    assert graph is not None and graph.path_source == "graph"
    assert graph.stop_ids == ["test:poi/east"]  # by the graph the 2nd stop breaks the budget


def test_plan_walk_checks_the_budget_on_the_graph(tmp_path) -> None:  # type: ignore[no-untyped-def]
    g = load_graph(write_graph(tmp_path, STREET, STREET_EDGES))
    east = cand_at("east", at(200, -5), score=0.9)
    north = cand_at("north", at(0, 135), score=0.5)  # 100 m north of the far sidewalk
    budget = 52  # two stops: straight loop 52 min, on the graph 53 min
    by_line = plan_walk(at(0, -5), [east, north], budget)
    on_graph = plan_walk(at(0, -5), [east, north], budget, graph=g)
    assert len(by_line) == 2
    assert [c.poi.id for c in on_graph] == ["test:poi/east"]


def test_graph_note_languages() -> None:
    for lang in ("ru", "kk", "en"):
        straight = route_note(lang)
        graph = route_note(lang, graph=True)
        assert graph != straight and "OSM" in graph
    assert "лестниц" in route_note("ru", graph=True, wheelchair=True)



def test_graph_search_crash_gives_exactly_the_straight_route(
    tmp_path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    from app.walkgraph import WalkGraph

    g = load_graph(write_graph(tmp_path, STREET, STREET_EDGES))
    east, north = cand_at("east", at(200, -5)), cand_at("north", at(0, 35))
    visits = {"test:poi/east": 10, "test:poi/north": 10}

    def boom(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("bug in the search")

    monkeypatch.setattr(WalkGraph, "_search", boom)
    on_graph = plan_route(at(0, -5), [east, north], 27, visits, graph=g)
    plain = plan_route(at(0, -5), [east, north], 27, visits)
    assert on_graph == plain and on_graph is not None and on_graph.path_source == "straight"
    walk = plan_walk(at(0, -5), [east, north], 55, graph=g)
    assert [c.poi.id for c in walk] == [c.poi.id for c in plan_walk(at(0, -5), [east, north], 55)]


def test_route_with_every_leg_straight_is_the_straight_route(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """S1 at Mega on the real graph: NU Park only by a detour > 3 x + 200 m, both legs fall
    back to straight segments -> path_source=straight and the straight note, not «graph»."""
    from .test_walkgraph import FOOTWAY

    nodes = {"A": (0, 0, 0), "A2": (0, 600, 0), "B2": (60, 600, 0), "B": (60, 0, 0)}
    edges = [("A", "A2", FOOTWAY), ("A2", "B2", FOOTWAY), ("B2", "B", FOOTWAY)]
    g = load_graph(write_graph(tmp_path, nodes, edges))
    park = cand_at("park", at(60, 0))
    on_graph = plan_route(at(0, 0), [park], None, {"test:poi/park": 30}, loop=True, graph=g,
                          graph_note="graph note")
    plain = plan_route(at(0, 0), [park], None, {"test:poi/park": 30}, loop=True)
    assert on_graph == plain and on_graph is not None
    assert on_graph.path_source == "straight" and on_graph.note != "graph note"


def test_graph_route_with_some_straight_legs_says_so(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """A stop 3 m from the start hangs on the same node: that leg is straight, the next one
    follows the graph -> path_source=graph and the note says some segments are straight."""
    g = load_graph(write_graph(tmp_path, STREET, STREET_EDGES))
    near, north = cand_at("near", at(2, -3)), cand_at("north", at(0, 35))
    visits = {"test:poi/near": 10, "test:poi/north": 10}
    for lang, sentence in (("ru", "Часть отрезков посчитана по прямой × 1,3."),
                           ("kk", "Кейбір кесінділер түзу сызықпен × 1,3 есептелген."),
                           ("en", "Some segments are straight lines × 1.3.")):
        r = plan_route(at(0, -5), [near, north], None, visits, graph=g, lang=lang,
                       graph_note=route_note(lang, graph=True))
        assert r is not None and r.path_source == "graph"
        assert r.stop_ids == ["test:poi/near", "test:poi/north"]
        assert r.legs[0].distance_m <= 4 and r.legs[1].distance_m > 200  # straight, graph
        assert r.note == f"{route_note(lang, graph=True)} {sentence}"
    whole = plan_route(at(0, -5), [cand_at("east", at(200, -5))], None, {}, graph=g)
    assert whole is not None and whole.path_source == "graph"
    assert "по прямой" not in whole.note  # every leg on the graph: the note stays as it was


# ------------------------------------------- wave 8 (T121): route profiles, 1.3.0 facts


STAIRS_FALLBACK = {
    "ru": "Без ступеней пути нет или он длиннее больше чем на 30 % — на маршруте есть ступени.",
    "kk": "Баспалдақсыз жол жоқ немесе ол 30 %-дан астам ұзын — маршрутта баспалдақ бар.",
    "en": "No step-free way, or it is over 30% longer: the route has steps.",
}


def test_graph_route_facts_by_default(tmp_path) -> None:  # type: ignore[no-untyped-def]
    g = load_graph(write_graph(tmp_path, STREET, STREET_EDGES))  # every way is lit=yes
    r = plan_route(at(0, -5), [cand_at("north", at(0, 35))], None, {}, graph=g)
    assert r is not None and r.path_source == "graph" and r.profile == "fastest"
    assert r.lit_share is not None and r.unlit_share == 0 and r.unknown_lit_share is not None
    assert r.lit_share == pytest.approx(230 / 240, abs=0.002)  # the two 5 m links: no data
    assert r.lit_share + r.unlit_share + r.unknown_lit_share == pytest.approx(1, abs=0.002)
    assert (r.kerb_lowered, r.kerb_unknown, r.steps_count) == (0, 1, 0)
    assert r.max_outdoor_leg_min == max(leg.walk_min for leg in r.legs)


def test_lit_shares_are_never_rounded_up_to_lit(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from .test_walkgraph import FOOTWAY, LIT

    nodes = {"A": (0, 0, 0), "B": (2999.9, 0, 0), "C": (3000, 0, 0)}
    g = load_graph(write_graph(tmp_path, nodes, [("A", "B", FOOTWAY | LIT), ("B", "C", FOOTWAY)]))
    r = plan_route(at(0, 0), [cand_at("c", at(3000, 0))], None, {}, graph=g)
    assert r is not None and r.lit_share is not None  # 99.99% lit: round() would say 100%
    assert r.lit_share == 0.999 and r.unknown_lit_share == 0.001


def test_kerbs_are_counted_per_crossing(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from .test_walkgraph import CROSS_SEG, CROSSING, KERB_RAISED, LOWERED, SIGNALS

    nodes = {"S": (0, 0, 0), "C1": (0, 15, CROSSING | LOWERED), "N": (0, 30, 0),
             "C2": (0, 45, CROSSING | SIGNALS), "M": (0, 60, 0),
             "C3": (0, 75, CROSSING | KERB_RAISED), "T": (0, 90, 0)}
    edges = [("S", "C1", CROSS_SEG), ("C1", "N", CROSS_SEG), ("N", "C2", CROSS_SEG),
             ("C2", "M", CROSS_SEG), ("M", "C3", CROSS_SEG), ("C3", "T", CROSS_SEG)]
    g = load_graph(write_graph(tmp_path, nodes, edges))
    stops = [cand_at("n", at(0, 30)), cand_at("t", at(0, 90))]
    r = plan_route(at(0, -1), stops, None, {}, graph=g)
    assert r is not None and len(r.crossings) == 3
    assert (r.kerb_lowered, r.kerb_unknown) == (1, 1)  # the raised one is neither


@pytest.mark.parametrize("lang", ["ru", "kk", "en"])
def test_accessible_route_with_stairs_says_why(tmp_path, lang) -> None:  # type: ignore[no-untyped-def]
    from .test_walkgraph import stairs_in_the_middle

    g = stairs_in_the_middle(tmp_path, None)  # no way round the stairs
    note = route_note(lang, graph=True)
    r = plan_route(at(0, 0), [cand_at("b", at(500, 0))], None, {}, graph=g,
                   profile="accessible", lang=lang, graph_note=note)
    assert r is not None and r.path_source == "graph"
    assert r.profile == "accessible" and r.steps_count == 1
    assert r.note == f"{note} {STAIRS_FALLBACK[lang]}"


def test_accessible_route_goes_round_the_stairs(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from .test_walkgraph import stairs_in_the_middle

    g = stairs_in_the_middle(tmp_path, 30)
    stops = [cand_at("b", at(500, 0))]
    fastest = plan_route(at(0, 0), stops, None, {}, graph=g)
    accessible = plan_route(at(0, 0), stops, None, {}, graph=g, profile="accessible",
                            graph_note="graph note")
    assert fastest is not None and accessible is not None
    assert (fastest.profile, fastest.steps_count) == ("fastest", 1)
    assert (accessible.profile, accessible.steps_count) == ("accessible", 0)
    assert accessible.total_walk_m > fastest.total_walk_m and accessible.note == "graph note"


def test_lit_route_shares(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from .test_walkgraph import UNLIT, lit_or_not

    g = lit_or_not(tmp_path, UNLIT, 130)
    stops = [cand_at("b", at(100, 0))]
    fastest = plan_route(at(0, 0), stops, None, {}, graph=g)
    lit = plan_route(at(0, 0), stops, None, {}, graph=g, profile="lit")
    assert fastest is not None and lit is not None and lit.profile == "lit"
    assert (fastest.lit_share, fastest.unlit_share) == (0, pytest.approx(1, abs=0.005))
    assert (lit.lit_share, lit.unlit_share) == (pytest.approx(1, abs=0.005), 0)


@pytest.mark.parametrize("lang", ["ru", "kk", "en"])
def test_straight_route_facts_and_profile(lang) -> None:  # type: ignore[no-untyped-def]
    stops = [NEAR, FAR]
    plain = plan_route(ORIGIN, stops, None, VISITS, note="note")
    assert plain is not None and plain.path_source == "straight"
    assert (plain.profile, plain.lit_share, plain.unlit_share, plain.unknown_lit_share) == (
        "fastest", 0, 0, 1)
    assert (plain.kerb_lowered, plain.kerb_unknown) == (None, None)  # crossings not known
    assert plain.max_outdoor_leg_min == max(leg.walk_min for leg in plain.legs)
    assert plain.note == "note"
    asked = plan_route(ORIGIN, stops, None, VISITS, note="note", profile="accessible", lang=lang)
    assert asked is not None and asked.profile == "fastest"  # what the route really is
    assert asked.note.startswith("note ") and asked.note != "note"


def test_plan_walk_checks_minutes_with_the_profile(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from app.walkgraph import WalkGraph

    g = load_graph(write_graph(tmp_path, STREET, STREET_EDGES))
    seen: list[str] = []
    real = WalkGraph.route_through

    def spy(self, points, mode=WALK, **kw):  # type: ignore[no-untyped-def]
        seen.append(mode.profile)
        return real(self, points, mode, **kw)

    monkeypatch.setattr(WalkGraph, "route_through", spy)
    east, north = cand_at("east", at(200, -5), score=0.9), cand_at("north", at(0, 135))
    plan_walk(at(0, -5), [east, north], 60, graph=g, profile="lit")
    assert seen and set(seen) == {"lit"}


def test_crossing_of_unknown_type_reaches_the_answer(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from .test_walkgraph import CROSS_SEG, CROSSING

    nodes = {"S": (0, 0, 0), "C": (0, 15, CROSSING), "N": (0, 30, 0)}
    g = load_graph(write_graph(tmp_path, nodes, [("S", "C", CROSS_SEG), ("C", "N", CROSS_SEG)]))
    r = plan_route(at(0, -1), [cand_at("n", at(0, 31))], None, {}, graph=g)
    assert r is not None and [c.kind for c in r.crossings] == ["unknown"]


STEPS_UNCHECKED = {
    "ru": "На прямых отрезках ступени не проверены.",
    "kk": "Түзу кесінділерде баспалдақ тексерілмеген.",
    "en": "Steps are not checked on the straight segments.",
}
PARTLY_STRAIGHT = {
    "ru": "Часть отрезков посчитана по прямой × 1,3.",
    "kk": "Кейбір кесінділер түзу сызықпен × 1,3 есептелген.",
    "en": "Some segments are straight lines × 1.3.",
}


@pytest.mark.parametrize("lang", ["ru", "kk", "en"])
def test_accessible_route_with_straight_segments_says_steps_are_not_checked(
    tmp_path, lang
) -> None:  # type: ignore[no-untyped-def]
    """steps_count sees only the graph legs: a step-free claim needs every leg on the graph."""
    g = load_graph(write_graph(tmp_path, STREET, STREET_EDGES))
    near, north = cand_at("near", at(2, -3)), cand_at("north", at(0, 35))
    visits = {"test:poi/near": 10, "test:poi/north": 10}
    note = route_note(lang, graph=True)
    r = plan_route(at(0, -5), [near, north], None, visits, graph=g, lang=lang, graph_note=note,
                   profile="accessible")
    assert r is not None and r.path_source == "graph" and r.profile == "accessible"
    assert r.legs[0].distance_m <= 4  # the straight leg
    assert r.note == f"{note} {PARTLY_STRAIGHT[lang]} {STEPS_UNCHECKED[lang]}"
    fastest = plan_route(at(0, -5), [near, north], None, visits, graph=g, lang=lang,
                         graph_note=note)
    assert fastest is not None and fastest.note == f"{note} {PARTLY_STRAIGHT[lang]}"
    whole = plan_route(at(0, -5), [cand_at("east", at(200, -5))], None, {}, graph=g, lang=lang,
                       graph_note=note, profile="accessible")
    assert whole is not None and whole.note == note  # every leg on the graph


# ------------------------------------------- wave 8.1 (T131): straight_share, last_stop_id


def test_straight_share_of_routes(tmp_path) -> None:  # type: ignore[no-untyped-def]
    g = load_graph(write_graph(tmp_path, STREET, STREET_EDGES))
    whole = plan_route(at(0, -5), [cand_at("east", at(200, -5))], None, {}, graph=g)
    assert whole is not None and whole.path_source == "graph"
    assert whole.straight_share == 0  # the 5 m links to the points belong to the graph legs
    near, north = cand_at("near", at(2, -3)), cand_at("north", at(0, 35))
    partly = plan_route(at(0, -5), [near, north], None, {}, graph=g)
    assert partly is not None and partly.path_source == "graph"
    straight_m = haversine_m(at(0, -5), at(2, -3))
    total_m = straight_m + sum(leg.distance_m for leg in partly.legs[1:])
    assert partly.straight_share == pytest.approx(straight_m / total_m, abs=0.002)
    assert 0 < partly.straight_share < 1
    plain = plan_route(ORIGIN, [NEAR, FAR], None, VISITS)
    assert plain is not None and plain.path_source == "straight" and plain.straight_share == 1


def test_a_tiny_straight_part_never_rounds_to_zero(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """«Без ступенек» is claimed only at straight_share == 0: 1 m in 3 km must stay > 0."""
    from .test_walkgraph import FOOTWAY

    nodes = {"A": (0, 0, 0), "B": (3000, 0, 0)}
    g = load_graph(write_graph(tmp_path, nodes, [("A", "B", FOOTWAY)]))
    stops = [cand_at("b", at(3000, 0)), cand_at("b2", at(3000, 1))]  # the 2nd leg: 1 m straight
    r = plan_route(at(0, 0), stops, None, {}, graph=g)
    assert r is not None and r.path_source == "graph" and len(r.legs) == 2
    assert r.straight_share == 0.001


def three_stops() -> list[Candidate]:
    # card order: far, near, mid; the best order from the origin is near, mid, far
    return [FAR, NEAR, MID]


def test_last_stop_goes_last_and_the_rest_keep_the_best_order() -> None:
    plain = plan_route(ORIGIN, three_stops(), None, VISITS)
    assert plain is not None
    assert plain.stop_ids == ["test:poi/near", "test:poi/mid", "test:poi/far"]
    event = plan_route(ORIGIN, three_stops(), None, VISITS, last_stop_id="test:poi/mid")
    assert event is not None  # near, far, mid: 0.015 deg < far, near, mid: 0.021 deg
    assert event.stop_ids == ["test:poi/near", "test:poi/far", "test:poi/mid"]
    assert [leg.to_id for leg in event.legs] == event.stop_ids
    loop = plan_route(ORIGIN, three_stops(), None, VISITS, loop=True,
                      last_stop_id="test:poi/near")
    assert loop is not None and loop.stop_ids[-1] == "test:poi/near"
    assert loop.legs[-1].to_id == "origin"  # the walk back still closes the loop
    unknown = plan_route(ORIGIN, three_stops(), None, VISITS, last_stop_id="test:poi/nowhere")
    assert unknown == plain


def test_last_stop_on_the_graph(tmp_path) -> None:  # type: ignore[no-untyped-def]
    g = load_graph(write_graph(tmp_path, STREET, STREET_EDGES))
    east, north = cand_at("east", at(200, -5)), cand_at("north", at(0, 35))
    plain = plan_route(at(0, -5), [east, north], None, {}, graph=g)
    assert plain is not None and plain.stop_ids == ["test:poi/north", "test:poi/east"]
    event = plan_route(at(0, -5), [east, north], None, {}, graph=g, last_stop_id="test:poi/north")
    assert event is not None and event.path_source == "graph"
    assert event.stop_ids == ["test:poi/east", "test:poi/north"]


def test_a_0_m_step_to_a_stop_where_you_stand_is_not_a_straight_part(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """E13 on the real graph: the first stop is Khan Shatyr itself, 0.3 m from the landmark.
    Shown as «0 м», that step is no walk: neither in straight_share nor in the note."""
    g = load_graph(write_graph(tmp_path, STREET, STREET_EDGES))
    here, north = cand_at("here", at(0.3, -5)), cand_at("north", at(0, 35))
    for profile in ("fastest", "accessible"):
        r = plan_route(at(0, -5), [here, north], None, {}, graph=g, graph_note="graph note",
                       profile=profile)  # type: ignore[arg-type]
        assert r is not None and r.path_source == "graph"
        assert r.legs[0].to_id == "test:poi/here" and r.legs[0].distance_m == 0
        assert r.straight_share == 0 and r.note == "graph note"


@pytest.mark.parametrize(("link_m", "share"), [(50, 0.0), (51, 0.093)])
def test_a_link_over_50_m_counts_as_straight(tmp_path, link_m, share) -> None:  # type: ignore[no-untyped-def]
    """Integrator: links to the walk graph up to 50 m (whole metres) are part of the graph leg;
    a longer one (up to 150 m, S3: 131 m) has unchecked steps: it counts whole as straight.
    51 m of 551 m = 0.0926 -> 0.093 (rounded up)."""
    from .test_walkgraph import FOOTWAY

    g = load_graph(write_graph(tmp_path, {"A": (0, 0, 0), "B": (500, 0, 0)}, [("A", "B", FOOTWAY)]))
    assert g is not None
    a, b = g.point(0), g.point(1)  # the stored (float32) node points
    m_per_deg = haversine_m(a, GeoPoint(lat=a.lat + 1e-3, lon=a.lon)) / 1e-3
    origin = GeoPoint(lat=a.lat - link_m / m_per_deg, lon=a.lon)  # link_m south of node A
    assert haversine_m(origin, a) == pytest.approx(link_m, abs=0.01)
    r = plan_route(origin, [cand_at("b", b)], None, {}, graph=g, graph_note="graph note")
    assert r is not None and r.path_source == "graph" and r.total_walk_m == 500 + link_m
    assert r.straight_share == share
    assert r.note == "graph note"  # the minutes are not x1.3: no «part straight» sentence
