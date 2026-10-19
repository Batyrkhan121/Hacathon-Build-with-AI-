"""T20: geo helpers — distances, walking time, H3, landmarks, search radius."""

from __future__ import annotations

import h3
import pytest

from app.geo import (
    DEFAULT_RADIUS_M,
    LANDMARK_H3_RES,
    MAX_RADIUS_M,
    MIN_RADIUS_M,
    h3_center,
    h3_resolution,
    haversine_m,
    hectares_text,
    match_landmark,
    radius_for,
    resolve_origin,
    to_h3,
    walk_minutes,
)
from app.schemas import GeoPoint, Landmark, LocationInput, QueryIntent

EXPO = GeoPoint(lat=51.0894, lon=71.4161)
BAITEREK = GeoPoint(lat=51.1283, lon=71.4305)


# ----------------------------------------------------------------- distances


def test_haversine_zero_and_symmetric() -> None:
    assert haversine_m(EXPO, EXPO) == 0
    assert haversine_m(EXPO, BAITEREK) == pytest.approx(haversine_m(BAITEREK, EXPO))


def test_haversine_known_distances() -> None:
    one_degree_lat = haversine_m(GeoPoint(lat=51.0, lon=71.0), GeoPoint(lat=52.0, lon=71.0))
    assert one_degree_lat == pytest.approx(111_195, rel=1e-3)
    assert haversine_m(EXPO, BAITEREK) == pytest.approx(4_440, rel=0.01)


@pytest.mark.parametrize(
    ("distance_m", "minutes"),
    [(0, 1), (10, 1), (57, 1), (750, 13), (1000, 18), (1050, 19), (1500, 26), (2500, 44)],
)
def test_walk_minutes(distance_m: float, minutes: int) -> None:
    assert walk_minutes(distance_m) == minutes


# ------------------------------------------------------------------------ H3


def test_h3_roundtrip() -> None:
    cell = to_h3(EXPO)
    assert h3.is_valid_cell(cell)
    assert h3_resolution(cell) == 9
    centre = h3_center(cell)
    assert haversine_m(centre, EXPO) < 250  # r9 edge is ~200 m
    assert to_h3(centre) == cell


def test_to_h3_other_resolution() -> None:
    assert h3_resolution(to_h3(EXPO, res=8)) == 8


# ----------------------------------------------------------------- landmarks


@pytest.mark.parametrize(
    ("text", "landmark_id"),
    [
        ("Я сейчас возле Mega Silk Way, хочу прогуляться 30–60 минут", "mega_silk_way"),
        ("мега", "mega_silk_way"),
        ("Встречаемся у Байтерека через час", "baiterek"),  # Russian case ending
        ("возле Хан-Шатыра", "khan_shatyr"),
        ("near Khan Shatyr please", "khan_shatyr"),
        ("Нұр Әлем жанында не бар?", "expo"),
        ("Что посмотреть у ЭКСПО?", "expo"),
        ("2 часа до вылета, я в аэропорту", "airport_nqz"),
    ],
)
def test_match_landmark_finds_alias(
    text: str, landmark_id: str, landmarks: dict[str, Landmark]
) -> None:
    found = match_landmark(text, landmarks)
    assert found is not None
    assert found.id == landmark_id


@pytest.mark.parametrize(
    "text",
    ["", "Живу в мегаполисе", "Интересный экспонат", "просто погулять рядом"],
)
def test_match_landmark_whole_words_only(text: str, landmarks: dict[str, Landmark]) -> None:
    assert match_landmark(text, landmarks) is None


def test_match_landmark_prefers_longest_alias() -> None:
    point = GeoPoint(lat=51.1, lon=71.4)
    lms = {
        "park": Landmark(id="park", names={"ru": "Парк"}, aliases=["парк"], geo=point),
        "lovers": Landmark(
            id="lovers", names={"ru": "Парк влюблённых"}, aliases=["парк влюбленных"], geo=point
        ),
    }
    found = match_landmark("Хочу в Парк влюблённых", lms)
    assert found is not None
    assert found.id == "lovers"


def test_resolve_origin_landmark(landmarks: dict[str, Landmark]) -> None:
    origin, cell, landmark_id, label = resolve_origin(LocationInput(landmark_id="expo"), landmarks)
    assert origin == landmarks["expo"].geo
    assert cell == to_h3(origin)
    assert landmark_id == "expo"
    assert label.startswith("у ")
    assert "EXPO" in label


def test_resolve_origin_cell_uses_centre_not_raw_point(landmarks: dict[str, Landmark]) -> None:
    cell = to_h3(EXPO)
    origin, out_cell, landmark_id, label = resolve_origin(LocationInput(h3_cell=cell), landmarks)
    assert origin == h3_center(cell)
    assert out_cell == cell
    assert landmark_id is None
    assert "ваш район" in label
    assert label.startswith("ваш район (≈13 га)"), label  # r9 in Astana: 12,6 ha
    assert "у EXPO" in label  # nearest landmark within walking distance


def test_resolve_origin_cell_far_from_landmarks(landmarks: dict[str, Landmark]) -> None:
    cell = to_h3(GeoPoint(lat=51.20, lon=71.30))
    _, _, _, label = resolve_origin(LocationInput(h3_cell=cell), landmarks)
    assert label.startswith("ваш район")
    assert " у " not in label


def test_resolve_origin_unknown_landmark(landmarks: dict[str, Landmark]) -> None:
    with pytest.raises(KeyError):
        resolve_origin(LocationInput(landmark_id="atlantis"), landmarks)


def test_resolve_origin_invalid_cell(landmarks: dict[str, Landmark]) -> None:
    with pytest.raises(ValueError):
        resolve_origin(LocationInput(h3_cell="000000000000000"), landmarks)


# -------------------------------------------------------------------- radius


@pytest.mark.parametrize(
    ("kwargs", "radius"),
    [
        ({}, DEFAULT_RADIUS_M),
        ({"max_walk_min": 10}, 577),
        ({"time_budget_min": 60}, 1212),
        ({"time_budget_min": 30}, 606),
        ({"max_walk_min": 10, "time_budget_min": 120}, 577),  # explicit walk limit wins
        ({"time_budget_min": 600}, MAX_RADIUS_M),
        ({"max_walk_min": 2}, MIN_RADIUS_M),
        ({"time_budget_min": 5}, MIN_RADIUS_M),
    ],
)
def test_radius_for(kwargs: dict[str, int], radius: int) -> None:
    assert radius_for(QueryIntent(raw_query="погулять", **kwargs)) == radius


def test_resolve_origin_rejects_cells_finer_than_r10(landmarks: dict[str, Landmark]) -> None:
    for res in (11, 15):  # r15 ~1 m²: effectively a raw coordinate
        fine = to_h3(EXPO, res=res)
        with pytest.raises(ValueError):  # the schema says no (422) ...
            LocationInput(h3_cell=fine)
        with pytest.raises(ValueError, match="r10"):  # ... and geo does not rely on it
            resolve_origin(LocationInput.model_construct(h3_cell=fine, landmark_id=None),
                           landmarks)
    for res in (8, 9, 10):  # coarser cells and r10 are fine
        cell = to_h3(EXPO, res=res)
        origin, out_cell, _, _ = resolve_origin(LocationInput(h3_cell=cell), landmarks)
        assert origin == h3_center(cell) and out_cell == cell


def test_resolve_origin_r10_label_in_hectares(landmarks: dict[str, Landmark]) -> None:
    cell = to_h3(EXPO, res=10)  # 1,79 ha in Astana; «0,0 км²» would say nothing
    _, _, _, label = resolve_origin(LocationInput(h3_cell=cell), landmarks)
    assert label.startswith("ваш район (≈1,8 га)"), label
    far = to_h3(GeoPoint(lat=51.20, lon=71.30), res=10)
    assert resolve_origin(LocationInput(h3_cell=far), landmarks)[3] == "ваш район (≈1,8 га)"


# same as the browser (web/app.js cellAreaHa + Intl.NumberFormat, maximumFractionDigits):
# from 10 ha a whole number, below one decimal at most (no trailing zero), half up
@pytest.mark.parametrize(("km2", "lang", "text"), [
    (0.8793, "ru", "88"), (0.1256, "ru", "13"), (0.0179, "ru", "1,8"), (0.0153, "kk", "1,5"),
    (0.0179, "en", "1.8"), (0.0200, "ru", "2"), (0.0996, "ru", "10"), (0.125, "ru", "13"),
    (0.0125, "ru", "1,3"), (125.0, "ru", "12 500"), (125.0, "en", "12,500"),
    (35.0, "ru", "3500"), (35.0, "en", "3,500")])
def test_hectares_text_matches_the_browser(km2: float, lang: str, text: str) -> None:
    assert hectares_text(km2, lang) == text


def test_landmark_cell_stays_r9(landmarks: dict[str, Landmark]) -> None:
    _, cell, _, _ = resolve_origin(LocationInput(landmark_id="expo"), landmarks)
    assert h3_resolution(cell) == LANDMARK_H3_RES == 9
