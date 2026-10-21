"""app/weather.py with httpx.MockTransport (no network)."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from app import weather
from app.hours import ASTANA_TZ
from app.schemas import GeoPoint

EXPO = GeoPoint(lat=51.0894, lon=71.4161)
CURRENT = {"temperature_2m": 4.3, "precipitation": 0.0, "wind_speed_10m": 9.2, "is_day": 1}
TIMEOUTS: list[float] = []  # timeout_s of every client made since the last install()


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

    TIMEOUTS.clear()

    def client(timeout_s: float = weather.TIMEOUT_S) -> httpx.Client:
        TIMEOUTS.append(timeout_s)
        return httpx.Client(transport=httpx.MockTransport(wrapped))

    monkeypatch.setattr(weather, "_make_client", client)
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
    assert params["latitude"] == "51.1" and params["longitude"] == "71.4"  # rounded origin
    assert params["hourly"] == weather.FIELDS and params["forecast_days"] == "2"
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


def test_a_request_asks_once_the_warm_up_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}

    def flaky(_r: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] <= 2:
            return httpx.Response(503)
        return httpx.Response(200, json={"current": CURRENT})

    install(monkeypatch, flaky)
    assert weather.get_weather(EXPO, now()) is None  # one try within the request timeout
    assert calls["n"] == 1 and weather.last_status() == "unavailable"
    assert TIMEOUTS == [weather.TIMEOUT_S]
    weather._cache.clear()
    assert weather.warm_up([EXPO]) == 1  # 503, then 200: the warm-up is patient
    assert calls["n"] == 3 and TIMEOUTS[1:] == [weather.WARMUP_TIMEOUT_S] * 2
    assert weather.get_weather(EXPO, now()) is not None and weather.last_status() == "cache"


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


@pytest.mark.parametrize("missing", ["precipitation", "temperature_2m"])
def test_missing_field_is_unknown_not_dry(monkeypatch: pytest.MonkeyPatch, missing: str) -> None:
    current = {k: v for k, v in CURRENT.items() if k != missing}
    install(monkeypatch, lambda r: httpx.Response(200, json={"current": current}))
    assert weather.get_weather(EXPO, now()) is None


def test_time_machine_helper() -> None:
    assert weather.is_time_machine(now() + timedelta(hours=4))
    assert not weather.is_time_machine(now() + timedelta(hours=1))


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


def test_naive_time_is_astana_local(monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch, lambda r: httpx.Response(200, json={"current": CURRENT}))
    naive_local = datetime.now(ASTANA_TZ).replace(tzinfo=None)
    assert weather.get_weather(EXPO, naive_local) is not None
    naive_utc_wrong = datetime.now(UTC).replace(tzinfo=None)  # 5 h off when read as local
    assert weather.get_weather(EXPO, naive_utc_wrong) is None



# ---------------------------------------------- T133: warm-up, 1.5 s timeout, the day's hours


def _hourly_answer(start: datetime, temps: list[float]) -> dict:
    hours = [(start + timedelta(hours=i)).strftime("%Y-%m-%dT%H:00") for i in range(len(temps))]
    return {"current": CURRENT, "hourly": {
        "time": hours, "temperature_2m": temps, "precipitation": [0.0] * len(temps),
        "wind_speed_10m": [3.0] * len(temps), "is_day": [1] * len(temps)}}


def test_the_request_timeout_is_one_and_a_half_seconds_in_all() -> None:
    assert weather.TIMEOUT_S == 1.5 and weather.ATTEMPTS == 1
    with weather._make_client() as client:
        t = client.timeout
        assert t.connect is not None and t.read is not None
        assert t.connect + t.read <= 1.5


def test_a_slow_service_gives_no_weather_and_says_so(monkeypatch: pytest.MonkeyPatch) -> None:
    def slow(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    seen = install(monkeypatch, slow)
    assert weather.get_weather(EXPO, now()) is None
    assert weather.last_status() == "unavailable" and len(seen) == 1
    weather.clear_status()
    assert weather.last_status() is None
    assert weather.get_weather(EXPO, now() + timedelta(hours=5)) is None
    assert weather.last_status() == "time_machine"


def test_the_days_forecast_answers_later_requests_without_the_network(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    start = datetime.now(UTC).replace(minute=0, second=0, microsecond=0) - timedelta(hours=2)
    temps = [float(t) for t in range(-20, -20 + 30)]
    seen = install(monkeypatch, lambda r: httpx.Response(200, json=_hourly_answer(start, temps)))
    assert weather.warm_up([EXPO, GeoPoint(lat=51.12, lon=71.43)]) == 1  # one area (0.1°)
    assert len(seen) == 1
    clock = weather.time.monotonic() + weather.CACHE_TTL_S + 60  # the current one is stale
    monkeypatch.setattr(weather.time, "monotonic", lambda: clock)
    at = now() + timedelta(hours=1)
    info = weather.get_weather(EXPO, at)
    assert info is not None and weather.last_status() == "cache" and len(seen) == 1
    hour = _as_hour(at)
    assert info.temp_c == temps[int((hour - start).total_seconds() // 3600)]


def _as_hour(at: datetime) -> datetime:
    return at.astimezone(UTC).replace(minute=0, second=0, microsecond=0)


def test_an_older_forecast_beats_nothing_when_the_service_is_down(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    start = datetime.now(UTC).replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)
    install(monkeypatch, lambda r: httpx.Response(200, json=_hourly_answer(start, [5.0] * 10)))
    weather.warm_up([EXPO])
    clock = weather.time.monotonic() + weather.DAY_TTL_S + 60  # even the forecast is old
    monkeypatch.setattr(weather.time, "monotonic", lambda: clock)
    install(monkeypatch, lambda r: httpx.Response(503))
    info = weather.get_weather(EXPO, now())
    assert info is not None and info.temp_c == 5.0 and weather.last_status() == "cache"


def test_warm_up_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch, lambda r: httpx.Response(500))
    assert weather.warm_up([EXPO]) == 0
    assert weather.warm_up([]) == 0


def test_a_broken_hourly_block_keeps_the_current_weather(monkeypatch: pytest.MonkeyPatch) -> None:
    answer = {"current": CURRENT, "hourly": {"time": ["not a time"], "temperature_2m": [1.0],
                                             "precipitation": [0.0]}}
    install(monkeypatch, lambda r: httpx.Response(200, json=answer))
    info = weather.get_weather(EXPO, now())
    assert info is not None and info.temp_c == 4.3
