"""app/weather.py with httpx.MockTransport (no network)."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from app import weather
from app.hours import ASTANA_TZ
from app.schemas import GeoPoint

EXPO = GeoPoint(lat=51.0894, lon=71.4161)
CURRENT = {"temperature_2m": 4.3, "precipitation": 0.0, "wind_speed_10m": 9.2, "is_day": 1}


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch) -> None:
    weather._cache.clear()
    monkeypatch.setattr(weather.time, "sleep", lambda _s: None)


def install(monkeypatch: pytest.MonkeyPatch, handler: Callable[[httpx.Request], httpx.Response]
            ) -> list[httpx.Request]:
    seen: list[httpx.Request] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    monkeypatch.setattr(
        weather, "_make_client", lambda: httpx.Client(transport=httpx.MockTransport(wrapped))
    )
    return seen


def now() -> datetime:
    return datetime.now(ASTANA_TZ)


def test_parses_current_weather(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = install(monkeypatch, lambda r: httpx.Response(200, json={"current": CURRENT}))
    info = weather.get_weather(EXPO, now())
    assert info is not None
    assert info.temp_c == 4.3 and info.wind_ms == 9.2 and info.is_day is True
    assert info.outdoor_ok is True
    assert info.summary == "+4°C, ветер 9 м/с"
    assert info.source == "open_meteo" and info.fetched_at.tzinfo is not None
    params = seen[0].url.params
    assert params["latitude"] == "51.09" and params["longitude"] == "71.42"  # rounded origin
    assert params["wind_speed_unit"] == "ms"


@pytest.mark.parametrize(
    ("current", "ok"),
    [
        ({"temperature_2m": -12.0, "precipitation": 0.0, "wind_speed_10m": 3.0}, False),
        ({"temperature_2m": 10.0, "precipitation": 1.2, "wind_speed_10m": 3.0}, False),
        ({"temperature_2m": 10.0, "precipitation": 0.0, "wind_speed_10m": 15.0}, False),
        ({"temperature_2m": -5.0, "precipitation": 0.0, "wind_speed_10m": 11.9}, True),
    ],
)
def test_outdoor_ok_rule(monkeypatch: pytest.MonkeyPatch, current: dict, ok: bool) -> None:
    install(monkeypatch, lambda r: httpx.Response(200, json={"current": current}))
    info = weather.get_weather(EXPO, now())
    assert info is not None and info.outdoor_ok is ok


def test_precipitation_in_summary(monkeypatch: pytest.MonkeyPatch) -> None:
    current = {"temperature_2m": -0.4, "precipitation": 0.6, "wind_speed_10m": 2.0}
    install(monkeypatch, lambda r: httpx.Response(200, json={"current": current}))
    info = weather.get_weather(EXPO, now())
    assert info is not None and info.summary == "+0°C, ветер 2 м/с, осадки 0,6 мм"


def test_cached_for_15_minutes(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = install(monkeypatch, lambda r: httpx.Response(200, json={"current": CURRENT}))
    weather.get_weather(EXPO, now())
    weather.get_weather(GeoPoint(lat=51.0891, lon=71.4159), now())  # same rounded key
    assert len(seen) == 1
    clock = weather.time.monotonic() + weather.CACHE_TTL_S + 1
    monkeypatch.setattr(weather.time, "monotonic", lambda: clock)
    weather.get_weather(EXPO, now())
    assert len(seen) == 2


def test_transient_failure_returns_without_online_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}

    def flaky(_r: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(503)
        return httpx.Response(200, json={"current": CURRENT})

    install(monkeypatch, flaky)
    assert weather.get_weather(EXPO, now()) is None
    assert calls["n"] == 1


@pytest.mark.parametrize(
    "handler",
    [
        lambda r: httpx.Response(500),
        lambda r: httpx.Response(200, text="not json"),
        lambda r: httpx.Response(200, json={"oops": 1}),
    ],
    ids=["http-500", "bad-json", "bad-shape"],
)
def test_failures_return_none(monkeypatch: pytest.MonkeyPatch, handler) -> None:
    install(monkeypatch, handler)
    assert weather.get_weather(EXPO, now()) is None


def test_network_error_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timeout", request=request)

    seen = install(monkeypatch, boom)
    assert weather.get_weather(EXPO, now()) is None
    assert len(seen) == weather.ATTEMPTS


def test_failure_is_cached_for_a_minute(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = install(monkeypatch, lambda r: httpx.Response(500))
    assert weather.get_weather(EXPO, now()) is None
    assert weather.get_weather(EXPO, now()) is None
    assert len(seen) == weather.ATTEMPTS  # the second call did not hit the network
    clock = weather.time.monotonic() + weather.FAILURE_TTL_S + 1
    monkeypatch.setattr(weather.time, "monotonic", lambda: clock)
    weather.get_weather(EXPO, now())
    assert len(seen) == 2 * weather.ATTEMPTS


def test_time_machine_skips_the_call(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = install(monkeypatch, lambda r: httpx.Response(200, json={"current": CURRENT}))
    assert weather.get_weather(EXPO, now() + timedelta(hours=5)) is None
    assert weather.get_weather(EXPO, now() - timedelta(days=1)) is None
    assert seen == []


def test_weather_respects_shared_request_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.llm import request_budget

    seen = install(monkeypatch, lambda r: httpx.Response(200, json={"current": CURRENT}))
    with request_budget(0):
        assert weather.get_weather(EXPO, now()) is None
    assert seen == []


def test_stalled_weather_cannot_hold_up_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    release = threading.Event()
    finished = threading.Event()

    def stalled(_lat: float, _lon: float) -> dict:
        release.wait(1)
        finished.set()
        return {"current": CURRENT}

    monkeypatch.setattr(weather, "_fetch", stalled)
    monkeypatch.setattr(weather, "TIMEOUT_S", 0.02)
    started = time.monotonic()
    try:
        assert weather.get_weather(EXPO, now()) is None
        assert time.monotonic() - started < 0.5
    finally:
        release.set()
        assert finished.wait(1)


def test_naive_time_is_astana_local(monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch, lambda r: httpx.Response(200, json={"current": CURRENT}))
    naive_local = datetime.now(ASTANA_TZ).replace(tzinfo=None)
    assert weather.get_weather(EXPO, naive_local) is not None
    naive_utc_wrong = datetime.now(UTC).replace(tzinfo=None)  # 5 h off when read as local
    assert weather.get_weather(EXPO, naive_utc_wrong) is None
