"""T24: guardrails between the LLM and the user + deterministic template cards."""

from __future__ import annotations

from datetime import datetime

import pytest

from app.geo import to_h3
from app.hours import ASTANA_TZ
from app.schemas import (
    POI,
    Candidate,
    CardDraft,
    DraftResponse,
    GeoPoint,
    HoursVerdict,
    QueryIntent,
    SearchContext,
)
from app.validate import (
    MAX_TODO_CHARS,
    MAX_WHY_CHARS,
    finalize,
    sanitize_summary,
    template_cards,
)

from .conftest import EXPO

ORIGIN = GeoPoint(lat=EXPO[0], lon=EXPO[1])
OPEN_HIGH = HoursVerdict(status="open", confidence="high", note="по данным OSM: ежедневно")
UNKNOWN = HoursVerdict(status="unknown", confidence="low", note="нет данных о часах работы")


def ctx() -> SearchContext:
    return SearchContext(
        origin=ORIGIN,
        h3_cell=to_h3(ORIGIN),
        h3_res=9,
        area_label="у EXPO",
        local_time=datetime(2026, 10, 20, 12, 0, tzinfo=ASTANA_TZ),
        radius_m=1200,
    )


def intent(lang: str = "ru", **kwargs: object) -> QueryIntent:
    return QueryIntent(raw_query="куда сходить", lang=lang, **kwargs)


def cand(poi: POI, hours: HoursVerdict = UNKNOWN, distance: int = 300) -> Candidate:
    walk = max(1, round(distance * 1.3 / 75))
    return Candidate(
        poi=poi,
        distance_m=distance,
        walk_min=walk,
        hours=hours,
        score=0.5,
        matched=[f"≈{walk} мин пешком", "есть кофе (OSM)", "тихое место"],
    )


def draft(poi_id: str, why: str = "Тихо и уютно.", todo: str = "Возьмите кофе.", **kw: object):
    data: dict[str, object] = {
        "poi_id": poi_id,
        "why": why,
        "what_to_do": todo,
        "visit_min_low": 20,
        "visit_min_high": 40,
        "idea_source": "generated",
    }
    data.update(kw)
    return CardDraft.model_validate(data)


def make_poi(poi_id: str, category: str, name: str | None = None, **kwargs: object) -> POI:
    data: dict[str, object] = {
        "id": f"test:poi/{poi_id}",
        "name": name or f"Место {poi_id}",
        "category": category,
        "geo": {"lat": EXPO[0] + 0.001, "lon": EXPO[1]},
        "sources": ["osm"],
        "data_confidence": 0.6,
        "fetched_at": "2026-10-18",
    }
    data.update(kwargs)
    return POI.model_validate(data)


@pytest.fixture
def cands(synthetic_pois: list[POI]) -> list[Candidate]:
    by_id = {p.id: p for p in synthetic_pois}
    return [
        cand(by_id["test:poi/1"], OPEN_HIGH, 250),
        cand(by_id["test:poi/3"], UNKNOWN, 150),
        cand(by_id["test:poi/7"], OPEN_HIGH, 300),
        cand(by_id["test:poi/4"], UNKNOWN, 900),
    ]


# ------------------------------------------------------------------ finalize


def test_drops_foreign_and_duplicate_ids(cands: list[Candidate]) -> None:
    resp = DraftResponse(
        summary="",
        cards=[draft("test:poi/999"), draft("test:poi/1"), draft("test:poi/1")],
    )
    cards, warnings = finalize(resp, cands, intent(), ctx())
    assert [c.poi_id for c in cards] == ["test:poi/1"]
    assert [c.rank for c in cards] == [1]
    assert any("не из списка" in w for w in warnings)


def test_facts_come_from_candidate(cands: list[Candidate]) -> None:
    resp = DraftResponse(summary="", cards=[draft("test:poi/3"), draft("test:poi/7")])
    cards, _ = finalize(resp, cands, intent(), ctx())
    museum = cards[0]
    source = cands[1]
    assert museum.title == source.poi.name
    assert museum.category == "museum"
    assert museum.geo == source.poi.geo
    assert (museum.distance_m, museum.walk_min) == (source.distance_m, source.walk_min)
    assert (museum.open_status, museum.open_confidence) == ("unknown", "low")
    assert museum.facts_sources == source.poi.sources
    assert museum.data_confidence == source.poi.data_confidence
    assert museum.idea_source == "generated"
    assert [c.rank for c in cards] == [1, 2]


def test_unknown_hours_note_tells_to_check(cands: list[Candidate]) -> None:
    resp = DraftResponse(summary="", cards=[draft("test:poi/3")])
    cards, _ = finalize(resp, cands, intent(), ctx())
    assert cards[0].open_note == "нет данных о часах, проверьте перед визитом"


@pytest.mark.parametrize(
    "claim",
    [
        "Открыто сейчас!",
        "Кафе СЕЙЧАС ОТКРЫТО.",
        "It is open now.",
        "Музей сейчас работает.",
        "Қазір ашық.",
    ],
)
def test_strips_open_claims_without_high_confidence(cands: list[Candidate], claim: str) -> None:
    resp = DraftResponse(
        summary="", cards=[draft("test:poi/3", why=f"{claim} Тихое место с выставкой.")]
    )
    cards, warnings = finalize(resp, cands, intent(), ctx())
    assert cards[0].why == "Тихое место с выставкой."
    assert any("открыто сейчас" in w for w in warnings)


def test_keeps_open_claim_when_open_high(cands: list[Candidate]) -> None:
    why = "Открыто сейчас. Тихо и уютно."
    resp = DraftResponse(summary="", cards=[draft("test:poi/1", why=why)])
    cards, warnings = finalize(resp, cands, intent(), ctx())
    assert cards[0].why == why
    assert not warnings


def test_empty_why_after_strip_uses_matched(cands: list[Candidate]) -> None:
    resp = DraftResponse(
        summary="",
        cards=[draft("test:poi/3", why="Открыто сейчас!", todo="Сейчас открыто, заходите.")],
    )
    cards, _ = finalize(resp, cands, intent(), ctx())
    assert cards[0].why == "≈3 мин пешком, есть кофе (OSM), тихое место."
    assert cards[0].what_to_do  # replaced by the category template, never empty
    assert "открыто" not in cards[0].what_to_do.lower()


def test_trims_texts(cands: list[Candidate]) -> None:
    long_why = "Очень длинная причина " * 30
    long_todo = "Сделайте это " * 60
    resp = DraftResponse(summary="", cards=[draft("test:poi/1", why=long_why, todo=long_todo)])
    cards, _ = finalize(resp, cands, intent(), ctx())
    assert len(cards[0].why) <= MAX_WHY_CHARS
    assert len(cards[0].what_to_do) <= MAX_TODO_CHARS
    assert cards[0].why.endswith("…")


def test_why_at_most_two_sentences(cands: list[Candidate]) -> None:
    why = "Первое. Второе! Третье? Четвёртое."
    resp = DraftResponse(summary="", cards=[draft("test:poi/1", why=why)])
    cards, _ = finalize(resp, cands, intent(), ctx())
    assert cards[0].why == "Первое. Второе!"


def test_visit_minutes_ordered(cands: list[Candidate]) -> None:
    resp = DraftResponse(
        summary="", cards=[draft("test:poi/1", visit_min_low=90, visit_min_high=30)]
    )
    cards, _ = finalize(resp, cands, intent(), ctx())
    assert (cards[0].visit_min_low, cards[0].visit_min_high) == (30, 90)


def test_no_valid_drafts_falls_back_to_templates(cands: list[Candidate]) -> None:
    resp = DraftResponse(summary="", cards=[draft("test:poi/999")])
    cards, warnings = finalize(resp, cands, intent(), ctx())
    assert 1 <= len(cards) <= 3
    assert all(c.idea_source == "poi_data" for c in cards)
    assert any("по данным" in w for w in warnings)


def test_no_candidates(cands: list[Candidate]) -> None:
    cards, warnings = finalize(DraftResponse(summary="", cards=[]), [], intent(), ctx())
    assert cards == []
    assert warnings


# ------------------------------------------------------------ template_cards


def test_template_cards_basic(cands: list[Candidate]) -> None:
    cards = template_cards(cands, intent(), ctx())
    assert len(cards) == 3
    assert [c.rank for c in cards] == [1, 2, 3]
    assert all(c.idea_source == "poi_data" for c in cards)
    first = cards[0]
    assert first.poi_id == "test:poi/1"
    assert first.why == "≈4 мин пешком, есть кофе (OSM), тихое место."
    assert first.what_to_do
    assert 5 <= first.visit_min_low <= first.visit_min_high
    assert first.distance_m == cands[0].distance_m


def test_template_cards_n(cands: list[Candidate]) -> None:
    assert len(template_cards(cands, intent(), ctx(), n=1)) == 1
    assert template_cards([], intent(), ctx()) == []


@pytest.mark.parametrize(("lang", "walk_word"), [("en", "min walk"), ("kk", "мин жаяу")])
def test_template_cards_language(cands: list[Candidate], lang: str, walk_word: str) -> None:
    cards = template_cards(cands, intent(lang=lang), ctx())
    assert walk_word in cards[0].why
    russian_todo = template_cards(cands, intent(), ctx())[0].what_to_do
    assert cards[0].what_to_do != russian_todo


def test_template_cards_category_diversity() -> None:
    pois = [make_poi(str(i), "cafe") for i in range(1, 4)] + [make_poi("9", "museum")]
    cards = template_cards([cand(p) for p in pois], intent(), ctx())
    assert [c.category for c in cards].count("cafe") == 2
    assert "museum" in [c.category for c in cards]


def test_template_cards_one_unnamed_playground() -> None:
    pois = [make_poi(str(i), "playground", "Детская площадка") for i in range(1, 5)]
    pois.append(make_poi("9", "park", "Парк"))
    pois.append(make_poi("10", "museum", "Музей"))
    cards = template_cards([cand(p) for p in pois], intent(), ctx())
    assert sum(c.title.startswith("Детская площадка") for c in cards) == 1


def test_template_cards_only_unnamed_playgrounds_gives_one_card() -> None:
    pois = [make_poi(str(i), "playground", "Детская площадка") for i in range(1, 5)]
    cards = template_cards([cand(p) for p in pois], intent(), ctx())
    assert len(cards) == 1
    assert cards[0].title.startswith("Детская площадка · ")


def test_finalize_drops_second_unnamed_playground() -> None:
    pois = [make_poi(str(i), "playground", "Детская площадка") for i in range(1, 3)]
    pois.append(make_poi("9", "park", "Парк"))
    resp = DraftResponse(
        summary="",
        cards=[draft("test:poi/1"), draft("test:poi/2"), draft("test:poi/9")],
    )
    cards, warnings = finalize(resp, [cand(p) for p in pois], intent(), ctx())
    assert [c.poi_id for c in cards] == ["test:poi/1", "test:poi/9"]
    assert any("повторных" in w for w in warnings)


def test_template_visit_fits_budget(cands: list[Candidate]) -> None:
    cards = template_cards(cands, intent(time_budget_min=30), ctx())
    assert all(c.visit_min_high <= 30 for c in cards)
    assert all(c.visit_min_low <= c.visit_min_high for c in cards)


# --------------------------------------------------------- review follow-ups


@pytest.mark.parametrize(
    "claim",
    [
        "Now open!",
        "Already open.",
        "Open until 10 PM.",
        "Заведение открыто до 22:00.",
        "Работает до 02:00.",
    ],
)
def test_strips_more_open_claims(cands: list[Candidate], claim: str) -> None:
    resp = DraftResponse(summary="", cards=[draft("test:poi/3", why=f"{claim} Тихое место.")])
    cards, _ = finalize(resp, cands, intent(), ctx())
    assert cards[0].why == "Тихое место."


def test_abbreviations_do_not_split_sentences(cands: list[Candidate]) -> None:
    why = "Находится на ул. Абая, рядом с парком. Есть кофе и тихий зал. Третье."
    resp = DraftResponse(summary="", cards=[draft("test:poi/1", why=why)])
    cards, _ = finalize(resp, cands, intent(), ctx())
    assert cards[0].why == "Находится на ул. Абая, рядом с парком. Есть кофе и тихий зал."


def test_sanitize_summary(cands: list[Candidate]) -> None:
    resp = DraftResponse(summary="", cards=[draft("test:poi/1"), draft("test:poi/3")])
    cards, _ = finalize(resp, cands, intent(), ctx())
    text = "Сейчас открыто 2 места. Рядом тихое кафе и музей."
    assert sanitize_summary(text, cards) == "Рядом тихое кафе и музей."
    open_cards = [c for c in cards if c.open_status == "open"]
    assert sanitize_summary(text, open_cards) == text  # all cards open/high -> keep


def test_template_visit_leaves_room_for_a_route() -> None:
    cards = template_cards([cand(make_poi("1", "mall"))], intent(time_budget_min=60), ctx())
    assert (cards[0].visit_min_low, cards[0].visit_min_high) == (20, 60)


# ------------------------------------------------ distinct titles (wave 3, QUALITY)


def test_unnamed_place_title_gets_distance() -> None:
    card = template_cards(
        [cand(make_poi("1", "playground", "Детская площадка"), distance=350)], intent(), ctx()
    )[0]
    assert card.title == "Детская площадка · 350 м"


def test_named_place_title_unchanged(cands: list[Candidate]) -> None:
    cards = template_cards(cands, intent(), ctx())
    assert cards[0].title == cands[0].poi.name


def test_same_named_places_get_distance_in_title() -> None:
    pois = [make_poi("1", "cafe", "Drinkit"), make_poi("2", "cafe", "Drinkit")]
    cs = [cand(pois[0], distance=700), cand(pois[1], distance=1278)]
    resp = DraftResponse(summary="", cards=[draft("test:poi/1"), draft("test:poi/2")])
    cards, _ = finalize(resp, cs, intent(), ctx())
    assert [c.title for c in cards] == ["Drinkit · 700 м", "Drinkit · 1,3 км"]


def test_titles_unique_even_at_same_distance() -> None:
    pois = [
        make_poi("1", "cafe", "Drinkit"),
        make_poi("2", "cafe", "Drinkit", address="ул. Абая, 5"),
    ]
    cards = template_cards([cand(p, distance=500) for p in pois], intent(), ctx())
    titles = [c.title.lower() for c in cards]
    assert len(set(titles)) == len(titles)


@pytest.mark.parametrize(
    ("lang", "near", "far"), [("en", "350 m", "1.3 km"), ("kk", "350 м", "1,3 км")]
)
def test_title_distance_units(lang: str, near: str, far: str) -> None:
    pois = [make_poi("1", "playground", "Детская площадка"), make_poi("2", "cafe", "Мю")]
    cs = [cand(pois[0], distance=350), cand(pois[1], distance=1250)]
    cards = template_cards(cs, intent(lang=lang), ctx())
    assert cards[0].title.endswith(near)
    twin = [cand(make_poi("3", "cafe", "Мю"), distance=1250), cs[1]]
    assert template_cards(twin, intent(lang=lang), ctx())[0].title.endswith(far)


@pytest.mark.parametrize(
    ("lang", "note"),
    [
        ("ru", "нет данных о часах, проверьте перед визитом"),
        ("kk", "жұмыс уақыты туралы дерек жоқ, барар алдында тексеріңіз"),
        ("en", "no opening hours data, check before you go"),
    ],
)
def test_unknown_hours_note_in_user_language(
    cands: list[Candidate], lang: str, note: str
) -> None:
    cards = template_cards(cands, intent(lang=lang), ctx())
    unknown = [c for c in cards if c.open_status == "unknown"]
    assert unknown
    assert all(c.open_note == note for c in unknown)
    resp = DraftResponse(summary="", cards=[draft("test:poi/3")])
    finalized, _ = finalize(resp, cands, intent(lang=lang), ctx())
    assert finalized[0].open_note == note
