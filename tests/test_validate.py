"""T24: guardrails between the LLM and the user + deterministic template cards."""

from __future__ import annotations

import re
from datetime import datetime

import pytest

from app.geo import to_h3
from app.hours import ASTANA_TZ, evaluate_hours, unknown_note
from app.retrieval import retrieve
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
from app.store import Store
from app.validate import (
    MAX_TODO_CHARS,
    MAX_WHY_CHARS,
    finalize,
    has_open_claim,
    sanitize_summary,
    template_cards,
)

from .conftest import EXPO


def make_store_for(pois: list[POI]) -> Store:
    return Store(pois=pois, by_id={p.id: p for p in pois}, landmarks={})

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
        cand(by_id["test:poi/3"].model_copy(update={"opening_hours": None}), UNKNOWN, 150),
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
    assert any("о текущей работе места" in w for w in warnings)  # item 8: no claim in warning
    assert not any("открыто сейчас" in w.lower() for w in warnings)


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
    why = "Рядом сквер им. Абая и парк. Есть кофе и тихий зал. Третье."
    resp = DraftResponse(summary="", cards=[draft("test:poi/1", why=why)])
    cards, _ = finalize(resp, cands, intent(), ctx())
    assert cards[0].why == "Рядом сквер им. Абая и парк. Есть кофе и тихий зал."


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


@pytest.mark.parametrize("lang", ["ru", "kk", "en"])
def test_unknown_hours_note_in_user_language(cands: list[Candidate], lang: str) -> None:
    note = unknown_note(lang)  # texts live in app.hours (FEATURES); validate only picks them
    cards = template_cards(cands, intent(lang=lang), ctx())
    unknown = [c for c in cards if c.open_status == "unknown"]
    assert unknown
    assert all(c.open_note == note for c in unknown)
    resp = DraftResponse(summary="", cards=[draft("test:poi/3")])
    finalized, _ = finalize(resp, cands, intent(lang=lang), ctx())
    assert finalized[0].open_note == note


@pytest.mark.parametrize("lang", ["ru", "kk", "en"])
def test_hours_note_is_passed_through_in_user_language(lang: str) -> None:
    """retrieval asks app.hours for the note in the user's language; validate keeps it."""
    at = datetime(2026, 10, 19, 23, 15, tzinfo=ASTANA_TZ)
    poi = make_poi("1", "cafe", "Мю", opening_hours="Mo-Su 10:00-24:00")
    q = intent(lang=lang)
    context = ctx().model_copy(update={"local_time": at})
    out = retrieve(make_store_for([poi]), q, context, None)
    expected = evaluate_hours("Mo-Su 10:00-24:00", at, lang=lang).note
    assert out[0].hours.note == expected
    assert template_cards(out, q, context)[0].open_note == expected
    if lang != "ru":
        assert "по данным OSM" not in expected


@pytest.mark.parametrize(
    ("lang", "name", "distance", "title"),
    [
        ("kk", "Детская площадка", 350, "Балалар алаңы · 350 м"),
        ("en", "Детская площадка", 350, "Playground · 350 m"),
        ("ru", "Детская площадка", 350, "Детская площадка · 350 м"),
        ("en", "Сквер", 1250, "Public garden · 1.3 km"),
        ("en", "Детская площадка у Хатико", 300, "Playground near Хатико"),
        ("kk", "Детская площадка у Хатико", 300, "Хатико маңындағы балалар алаңы"),
        ("ru", "Детская площадка у Хатико", 300, "Детская площадка у Хатико"),
    ],
)
def test_unnamed_place_titles_are_translated(
    lang: str, name: str, distance: int, title: str
) -> None:
    category = "park" if name == "Сквер" else "playground"
    poi = make_poi("1", category, name)
    card = template_cards([cand(poi, distance=distance)], intent(lang=lang), ctx())[0]
    assert card.title == title


def test_own_translation_wins_over_generic_title() -> None:
    poi = make_poi("1", "playground", "Детская площадка", name_i18n={"en": "Rainbow playground"})
    assert template_cards([cand(poi)], intent(lang="en"), ctx())[0].title == "Rainbow playground"


@pytest.mark.parametrize("lang", ["ru", "kk", "en"])
def test_indoor_attraction_gets_indoor_advice(lang: str) -> None:
    """Rides inside Khan Shatyr are attractions with indoor=True: no «оденьтесь по погоде»."""
    ride = make_poi("1", "attraction", "Gold Mine Coaster", indoor=True)
    square = make_poi("2", "attraction", "Монумент", indoor=False)
    inside = template_cards([cand(ride)], intent(lang=lang), ctx())[0].what_to_do
    outside = template_cards([cand(square)], intent(lang=lang), ctx())[0].what_to_do
    assert inside != outside
    assert not any(w in inside for w in ("оденьтесь", "киініңіз", "dress"))



# ------------------------------------- RAG review REQUEST (integrator, wave 3 QUALITY)

OPEN_CLAIM_PHRASES = [
    # ru: plural, «в данный момент», «ещё», «сегодня до», 24/7, «с … до …»
    "Открыты сейчас до вечера.",
    "Все места открыты сейчас.",
    "В данный момент открыто.",
    "Всё ещё открыто.",
    "Ещё открыто.",
    "Открыто сегодня до 22:00.",
    "Круглосуточно.",
    "Работает 24/7.",
    "Работает с 9 до 22.",
    "Сейчас работает.",
    "Кафе СЕЙЧАС ОТКРЫТО.",
    # en
    "Open 24/7.",
    "We're open.",
    "Currently operating.",
    "Now open!",
    "Open until 10 PM.",
    "It is open now.",
    # kk
    "Қазір ашық.",
    "Мұражай ашық.",
    "Жұмыс істеп тұр.",
    "Қазір жұмыс істеп тұр.",
    # obfuscated: zero-width, Latin homoglyphs, fullwidth / compatibility forms
    "Откры\u200bто сейчас.",
    "Oткрыто сейчас.",
    "Открытo сейчас.",
    "\uff2fpen now.",
]
NOT_CLAIMS = [
    "Тихое место с видом на сферу.",
    "Открытая площадка у фонтана.",
    "Удобно работать с ноутбуком.",
    "Opening hours are on the card.",
    "Ашық аспан астында серуендеңіз.",
    "≈5 мин пешком, работает допоздна.",
    "≈5 min walk, open late.",
]


@pytest.mark.parametrize("claim", OPEN_CLAIM_PHRASES)
def test_open_claims_variants_are_stripped(cands: list[Candidate], claim: str) -> None:
    assert has_open_claim(claim)
    resp = DraftResponse(summary="", cards=[draft("test:poi/3", why=f"{claim} Тихое место.")])
    cards, _ = finalize(resp, cands, intent(), ctx())
    assert cards[0].why == "Тихое место."


@pytest.mark.parametrize("text", NOT_CLAIMS)
def test_harmless_sentences_are_not_claims(text: str) -> None:
    assert not has_open_claim(text)


def test_summary_plural_claim_is_stripped(cands: list[Candidate]) -> None:
    cards = template_cards(cands, intent(), ctx())  # mix of open/high and unknown
    text = "Все места открыты сейчас. Рядом тихое кафе и музей."
    assert sanitize_summary(text, cards) == "Рядом тихое кафе и музей."


@pytest.mark.parametrize(
    "bad",
    [
        "Средний чек 2500 тенге.",
        "Цены от $10.",
        "Звоните +7 777 123 45 67.",
        "Адрес: ул. Другая 99.",
        "Сайт www.example.kz.",
        "Всего 50 м от вас.",
    ],
)
def test_unverifiable_facts_are_stripped(cands: list[Candidate], bad: str) -> None:
    resp = DraftResponse(summary="", cards=[draft("test:poi/1", why=f"{bad} Тихо и уютно.")])
    cards, warnings = finalize(resp, cands, intent(), ctx())
    assert cards[0].why == "Тихо и уютно."
    assert any("непроверяем" in w for w in warnings)


def test_numbers_from_the_data_are_kept(cands: list[Candidate]) -> None:
    alpha = cands[0]  # 250 m, walk 4 min
    why = f"Всего {alpha.distance_m} м, около {alpha.walk_min} минут пешком. Тихо."
    resp = DraftResponse(summary="", cards=[draft("test:poi/1", why=why, visit_min_low=20,
                                                  visit_min_high=40,
                                                  todo="Посидите минут 20–40.")])
    cards, _ = finalize(resp, cands, intent(), ctx())
    assert cards[0].why == why
    assert cards[0].what_to_do == "Посидите минут 20–40."


def test_rounded_distance_is_kept(cands: list[Candidate]) -> None:
    resp = DraftResponse(summary="", cards=[draft("test:poi/4", why="Парк примерно в 900 метрах.")])
    cards, _ = finalize(resp, cands, intent(), ctx())  # candidate distance is 900 m
    assert cards[0].why == "Парк примерно в 900 метрах."


def test_open_high_wrong_closing_time_is_stripped(cands: list[Candidate]) -> None:
    hours = HoursVerdict(status="open", confidence="high",
                         note="по данным OSM: ежедневно 08:00–22:00")
    cs = [cand(cands[0].poi, hours, 250)]
    wrong = DraftResponse(summary="", cards=[draft("test:poi/1", why="Открыто до 23:00. Тихо.")])
    assert finalize(wrong, cs, intent(), ctx())[0][0].why == "Тихо."
    right = DraftResponse(summary="", cards=[draft("test:poi/1", why="Открыто до 22:00. Тихо.")])
    assert finalize(right, cs, intent(), ctx())[0][0].why == "Открыто до 22:00. Тихо."


def test_llm_summary_is_checked_and_short(cands: list[Candidate]) -> None:
    cards = template_cards(cands, intent(), ctx())
    text = "Вот три тихих места рядом. Средний чек 2500 тенге. Ещё одно предложение."
    assert sanitize_summary(text, cards, llm=True) == "Вот три тихих места рядом."
    assert len(sanitize_summary("Очень длинно " * 150 + ".", cards, llm=True)) <= 200
    # pipeline's own template summaries (with counts and times) are not fact-filtered
    own = "Подборка по открытым данным: 3 идеи рядом."
    assert sanitize_summary(own, cards) == own


def test_llm_summary_wrong_time_stripped_even_if_all_open(cands: list[Candidate]) -> None:
    hours = HoursVerdict(status="open", confidence="high",
                         note="по данным OSM: ежедневно 08:00–22:00")
    cards = template_cards([cand(cands[0].poi, hours, 250)], intent(), ctx())
    text = "Всё открыто до 23:00. Тихое кафе рядом."
    assert sanitize_summary(text, cards, llm=True) == "Тихое кафе рядом."


def test_poi_data_label_needs_support_in_data(cands: list[Candidate]) -> None:
    no_desc = [cand(c.poi.model_copy(update={"description": None}), c.hours, c.distance_m)
               for c in cands]
    invented = draft("test:poi/3", why="Здесь проходят мастер-классы по керамике.",
                     idea_source="poi_data")
    supported = draft("test:poi/1", why="Тихо, есть кофе и розетки.", idea_source="poi_data")
    cards, _ = finalize(DraftResponse(summary="", cards=[invented, supported]), no_desc,
                        intent(), ctx())
    by_id = {c.poi_id: c.idea_source for c in cards}
    assert by_id["test:poi/3"] == "generated"  # no description, why not backed by name/tags
    assert by_id["test:poi/1"] == "poi_data"


def test_poi_data_kept_when_place_has_description(cands: list[Candidate]) -> None:
    with_desc = cands[1].poi.model_copy(update={"description": "Музей энергии будущего."})
    cs = [cand(with_desc, UNKNOWN, 150)]
    d = draft("test:poi/3", why="Интересная экспозиция про будущее.", idea_source="poi_data")
    cards, _ = finalize(DraftResponse(summary="", cards=[d]), cs, intent(), ctx())
    assert cards[0].idea_source == "poi_data"


def test_poi_id_with_spaces_is_accepted(cands: list[Candidate]) -> None:
    cards, warnings = finalize(DraftResponse(summary="", cards=[draft(" test:poi/1 ")]), cands,
                               intent(), ctx())
    assert [c.poi_id for c in cards] == ["test:poi/1"]
    assert not any("не из списка" in w for w in warnings)


# ------------------------------------- silent-failure review №2: warnings follow lang


@pytest.mark.parametrize("lang", ["kk", "en"])
def test_finalize_warnings_follow_language(cands: list[Candidate], lang: str) -> None:
    resp = DraftResponse(
        summary="",
        cards=[
            draft("test:poi/999"),
            draft("test:poi/3", why="Открыто сейчас. Цены от 2500 тенге. Тихо."),
            draft("test:poi/3"),
        ],
    )
    _, warnings = finalize(resp, cands, intent(lang=lang), ctx())
    assert len(warnings) == 4  # foreign, duplicate, open claim, unverifiable fact
    for w in warnings:
        if lang == "en":
            assert not re.search("[а-яё]", w.lower()), w
        else:
            assert re.search("[әғқңөұүһі]", w.lower()), w
        assert not has_open_claim(w)
    _, empty = finalize(DraftResponse(summary="", cards=[]), [], intent(lang=lang), ctx())
    _, fallback = finalize(DraftResponse(summary="", cards=[draft("x:1/1")]), cands,
                           intent(lang=lang), ctx())
    for w in [*empty, *fallback]:
        assert (not re.search("[а-яё]", w.lower())) if lang == "en" else re.search("[әқң]", w)


# ------------------------------------- silent-failure review №2, item 3 (new parts)


def _open_high(note: str) -> HoursVerdict:
    return HoursVerdict(status="open", confidence="high", note=note)


def test_all_day_claim_needs_24_7_data(cands: list[Candidate]) -> None:
    day = [cand(cands[0].poi, _open_high("по данным OSM: ежедневно 08:00–22:00"), 250)]
    why = "Работает круглосуточно. Тихо."
    resp = DraftResponse(summary="", cards=[draft("test:poi/1", why=why)])
    assert finalize(resp, day, intent(), ctx())[0][0].why == "Тихо."
    allday_poi = cands[0].poi.model_copy(update={"tags": [*cands[0].poi.tags, "24_7"]})
    allday = [cand(allday_poi, _open_high("по данным OSM: круглосуточно"), 250)]
    assert finalize(resp, allday, intent(), ctx())[0][0].why == "Работает круглосуточно. Тихо."


@pytest.mark.parametrize(
    ("why", "kept"),
    [
        ("Закрывается поздно. Тихо.", "Тихо."),
        ("Открывается рано утром. Тихо.", "Тихо."),
        ("It closes late. Тихо.", "Тихо."),
        ("Вечером открывается вид на город. Тихо.", "Вечером открывается вид на город. Тихо."),
    ],
)
def test_open_close_words_need_known_hours(cands: list[Candidate], why: str, kept: str) -> None:
    resp = DraftResponse(summary="", cards=[draft("test:poi/3", why=why)])  # hours unknown
    assert finalize(resp, cands, intent(), ctx())[0][0].why == kept


def test_open_close_words_kept_when_hours_known(cands: list[Candidate]) -> None:
    resp = DraftResponse(summary="", cards=[draft("test:poi/1", why="Закрывается поздно. Тихо.")])
    assert finalize(resp, cands, intent(), ctx())[0][0].why == "Закрывается поздно. Тихо."


# ------------------------- review BLOCK: pipeline template summaries must stay intact

_TEMPLATE_VALUES = {"n": 3, "b": 120, "tr": 40, "res": 30, "w": 50, "hhmm": "14:20",
                    "dur": "2 ч"}


def _template_summaries() -> list[tuple[str, str, str]]:
    from app.pipeline import TEXTS  # FEATURES texts, read only

    out = []
    for lang, texts in TEXTS.items():
        for key, text in texts.items():
            if key.startswith(("summary", "airport")):
                out.append((lang, key, text.format(**_TEMPLATE_VALUES)))
    return out


@pytest.mark.parametrize(("lang", "key", "text"), _template_summaries())
def test_pipeline_template_summaries_are_untouched(
    cands: list[Candidate], lang: str, key: str, text: str
) -> None:
    mixed = template_cards(cands, intent(lang=lang), ctx())  # open/high + unknown
    assert sanitize_summary(text, []) == text
    assert sanitize_summary(text, mixed) == text
    assert not has_open_claim(text), (lang, key)


def test_typography_is_kept_in_kept_sentences(cands: list[Candidate]) -> None:
    text = "Три идеи рядом… Кафе №1 и сквер."
    assert sanitize_summary(text, []) == text  # no NFKC rewrite of «…» / «№»


@pytest.mark.parametrize(
    "claim",
    [
        "Открыто до закрытия.",
        "Открыто до позднего вечера.",
        "Открыт до поздней ночи.",
        "Открыта до последнего посетителя.",
        "Работает до закрытия.",
        "Open until closing.",
        "Кешке дейін ашық.",
    ],
)
def test_until_phrases_without_numbers_are_claims(claim: str) -> None:
    assert has_open_claim(claim)


@pytest.mark.parametrize(
    "text", ["Работает с 3 людьми.", "Работает допоздна.", "Ашық деректер бойынша 3 ұсыныс."]
)
def test_not_claims_after_review(text: str) -> None:
    assert not has_open_claim(text)


def test_all_day_reason_is_data_when_tagged() -> None:
    assert has_open_claim("≈20 мин пешком, круглосуточно.")
    assert not has_open_claim("≈20 мин пешком, круглосуточно.", all_day_ok=True)
    assert not has_open_claim("≈20 мин жаяу, тәулік бойы.", all_day_ok=True)


@pytest.mark.parametrize(
    ("why", "kept"),
    [
        ("Кафе на улице Абая. Тихо.", "Тихо."),
        ("Рядом по проспекту Мангилик Ел. Тихо.", "Тихо."),
        (
            "На улице холодно, зайдите погреться. Тихо.",
            "На улице холодно, зайдите погреться. Тихо.",
        ),
    ],
)
def test_street_names_in_any_case(cands: list[Candidate], why: str, kept: str) -> None:
    resp = DraftResponse(summary="", cards=[draft("test:poi/1", why=why)])
    assert finalize(resp, cands, intent(), ctx())[0][0].why == kept


def test_km_with_dot_is_not_a_time(cands: list[Candidate]) -> None:
    far = [cand(cands[3].poi, UNKNOWN, 1200)]
    resp = DraftResponse(summary="", cards=[draft("test:poi/4", why="Парк в 1.20 км. Тихо.")])
    assert finalize(resp, far, intent(), ctx())[0][0].why == "Парк в 1.20 км. Тихо."
