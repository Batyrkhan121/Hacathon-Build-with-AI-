"""Current weather from Open-Meteo (no API key). Owner: LLM lane (task T32); wave 8.1: FEATURES
(T133).

GET https://api.open-meteo.com/v1/forecast?latitude=..&longitude=..
    &current=temperature_2m,precipitation,wind_speed_10m,is_day
    &hourly=temperature_2m,precipitation,wind_speed_10m,is_day&forecast_days=2&wind_speed_unit=ms
- one answer = the current weather and today's (and tomorrow's) forecast by the hour;
- warm_up(points) at server start (api lifespan, a background thread) loads them for the
  landmark areas, patiently (WARMUP_TIMEOUT_S, WARMUP_ATTEMPTS);
- a request then takes: the current weather while it is fresh (CACHE_TTL_S), else the forecast
  for its hour while the day's answer is recent (DAY_TTL_S), else asks Open-Meteo once with
  TIMEOUT_S (≈1,5 s in all: half for the connection, half for the answer);
- no answer in time / any failure -> None and last_status() == "unavailable": the answer says
  «погода: нет данных (сервис не ответил)» (app.pipeline); a failure is remembered for
  FAILURE_TTL_S so a dead service costs at most one wait a minute;
- if `at` differs from now by more than 3 hours (demo "time machine") -> None ("time_machine").
outdoor_ok = temp >= -5 and precipitation < 0.5 and wind < 12 (tune later).

The coordinates sent are the search origin (H3 cell centre or landmark) rounded to
KEY_DECIMALS (≈11 × 7 km): one weather per part of the city, never a raw user point.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import httpx

from app.hours import ASTANA_TZ
from app.schemas import GeoPoint, Lang, WeatherInfo

log = logging.getLogger(__name__)

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
TIMEOUT_S = 1.5  # T133: a request never waits longer for the weather (connect + read)
ATTEMPTS = 1  # in a request: one try; the warm-up at start retries
WARMUP_TIMEOUT_S = 5.0
WARMUP_ATTEMPTS = 2
RETRY_PAUSE_S = 0.5
KEY_DECIMALS = 1  # ≈ 11 km × 7 km in Astana: the weather does not change within it
CACHE_TTL_S = 15 * 60  # the current weather
DAY_TTL_S = 3 * 3600  # the hourly forecast of the same answer
FAILURE_TTL_S = 60  # while Open-Meteo is down, do not wait on every request
MAX_SKEW = timedelta(hours=3)
MIN_OUTDOOR_TEMP_C = -5.0
MAX_OUTDOOR_PRECIP_MM = 0.5
MAX_OUTDOOR_WIND_MS = 12.0
FIELDS = "temperature_2m,precipitation,wind_speed_10m,is_day"


@dataclass
class _Entry:
    stamp: float  # time.monotonic() of the answer (or of the failure)
    current: WeatherInfo | None  # None = the last ask failed
    hourly: dict[datetime, WeatherInfo] = field(default_factory=dict)  # by UTC hour


_cache: dict[tuple[float, float], _Entry] = {}
_cache_lock = threading.Lock()
_local = threading.local()


def last_status() -> str | None:
    """How the last get_weather in this thread went: "ok" (asked now), "cache",
    "time_machine", "unavailable" (no answer in time / failure); None before any call."""
    return getattr(_local, "status", None)


def clear_status() -> None:
    _local.status = None


def is_time_machine(at: datetime) -> bool:
    """`at` is too far from now: the current weather would describe another moment."""
    return abs(_as_utc(at) - datetime.now(UTC)) > MAX_SKEW


def _key(origin: GeoPoint) -> tuple[float, float]:
    return round(origin.lat, KEY_DECIMALS), round(origin.lon, KEY_DECIMALS)


def get_weather(origin: GeoPoint, at: datetime) -> WeatherInfo | None:
    _local.status = None
    key = _key(origin)
    hit: _Entry | None = None
    try:
        if is_time_machine(at):
            _local.status = "time_machine"  # current weather would be misleading
            return None
        with _cache_lock:
            hit = _cache.get(key)
        if hit is not None:
            age = time.monotonic() - hit.stamp
            if hit.current is not None and age < CACHE_TTL_S:
                _local.status = "cache"
                return hit.current
            slot = _slot(hit, at)
            if slot is not None and age < DAY_TTL_S:
                _local.status = "cache"
                return slot
            if hit.current is None and age < FAILURE_TTL_S:
                _local.status = "cache" if slot is not None else "unavailable"
                return slot
        entry = _download(key, TIMEOUT_S, ATTEMPTS)
    except Exception as exc:  # noqa: BLE001 - weather is optional context
        log.warning("weather unavailable: %s", type(exc).__name__)
        entry = _Entry(time.monotonic(), None, dict(hit.hourly) if hit is not None else {})
        with _cache_lock:
            _cache[key] = entry
        slot = _slot(entry, at)  # an older forecast for this hour beats nothing
        _local.status = "cache" if slot is not None else "unavailable"
        return slot
    with _cache_lock:
        _cache[key] = entry
    _local.status = "ok"
    return entry.current


def warm_up(points: Iterable[GeoPoint]) -> int:
    """Load today's weather for these areas (server start, a background thread): later
    requests are answered from the cache. Never raises; returns how many areas loaded."""
    keys = sorted({_key(p) for p in points})
    loaded = 0
    for key in keys:
        try:
            entry = _download(key, WARMUP_TIMEOUT_S, WARMUP_ATTEMPTS)
        except Exception as exc:  # noqa: BLE001 - a request will ask again, with its timeout
            log.warning("weather warm-up failed for an area: %s", type(exc).__name__)
            continue
        with _cache_lock:
            _cache[key] = entry
        loaded += 1
    log.info("weather warm-up: %d of %d areas", loaded, len(keys))
    return loaded


def _slot(entry: _Entry, at: datetime) -> WeatherInfo | None:
    hour = _as_utc(at).replace(minute=0, second=0, microsecond=0)
    return entry.hourly.get(hour)


def _as_utc(at: datetime) -> datetime:
    if at.tzinfo is None:
        at = at.replace(tzinfo=ASTANA_TZ)
    return at.astimezone(UTC)


def _make_client(timeout_s: float = TIMEOUT_S) -> httpx.Client:
    half = timeout_s / 2  # connect + read ≈ timeout_s in all
    return httpx.Client(timeout=httpx.Timeout(half), headers={"User-Agent": "QadamHackathon/0.1"})


def _download(key: tuple[float, float], timeout_s: float, attempts: int) -> _Entry:
    data = _fetch(*key, timeout_s=timeout_s, attempts=attempts)
    return _Entry(time.monotonic(), _to_info(data), _hourly(data))


def _fetch(lat: float, lon: float, *, timeout_s: float = TIMEOUT_S,
           attempts: int = ATTEMPTS) -> dict:
    params = {
        "latitude": lat,
        "longitude": lon,
        "current": FIELDS,
        "hourly": FIELDS,
        "forecast_days": 2,  # the hours after midnight too
        "wind_speed_unit": "ms",
        "timezone": "UTC",
    }
    last: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            with _make_client(timeout_s) as client:
                resp = client.get(OPEN_METEO_URL, params=params)
                resp.raise_for_status()
                return resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            last = exc
            if attempt < attempts:
                time.sleep(RETRY_PAUSE_S)
    raise RuntimeError("Open-Meteo request failed") from last


def _info(temp: float, precip: float, wind: float | None, is_day: object,
          fetched_at: datetime) -> WeatherInfo:
    outdoor_ok = (
        temp >= MIN_OUTDOOR_TEMP_C
        and precip < MAX_OUTDOOR_PRECIP_MM
        and (wind is None or wind < MAX_OUTDOOR_WIND_MS)
    )
    return WeatherInfo(
        temp_c=temp,
        precipitation_mm=precip,
        wind_ms=wind,
        is_day=None if is_day is None else bool(is_day),
        summary=_summary(temp, precip, wind),
        outdoor_ok=outdoor_ok,
        fetched_at=fetched_at,
    )


def _to_info(data: dict) -> WeatherInfo:
    cur = data["current"]
    if cur.get("temperature_2m") is None or cur.get("precipitation") is None:
        # a missing field is "unknown", not "0 mm" (that would claim it is dry)
        raise ValueError("Open-Meteo answer without temperature or precipitation")
    wind = cur.get("wind_speed_10m")
    return _info(float(cur["temperature_2m"]), float(cur["precipitation"]),
                 None if wind is None else float(wind), cur.get("is_day"), datetime.now(UTC))


def _hourly(data: dict) -> dict[datetime, WeatherInfo]:
    """The forecast by UTC hour; hours without temperature or precipitation are skipped, a
    broken block gives no forecast (the current weather still counts)."""
    block = data.get("hourly")
    if not isinstance(block, dict):
        return {}
    out: dict[datetime, WeatherInfo] = {}
    fetched = datetime.now(UTC)
    try:
        times = block["time"]
        temps, precs = block["temperature_2m"], block["precipitation"]
        winds = block.get("wind_speed_10m") or [None] * len(times)
        days = block.get("is_day") or [None] * len(times)
        for i, stamp in enumerate(times):
            if temps[i] is None or precs[i] is None:
                continue
            hour = datetime.fromisoformat(stamp).replace(tzinfo=UTC)
            wind = winds[i]
            out[hour] = _info(float(temps[i]), float(precs[i]),
                              None if wind is None else float(wind), days[i], fetched)
    except (KeyError, IndexError, TypeError, ValueError):
        return {}
    return out


_WORDS = {
    "ru": ("ветер {w} м/с", "осадки {p} мм", ","),
    "kk": ("жел {w} м/с", "жауын-шашын {p} мм", ","),
    "en": ("wind {w} m/s", "precipitation {p} mm", "."),
}


def _summary(temp: float, precip: float, wind: float | None, lang: str = "ru") -> str:
    wind_fmt, precip_fmt, sep = _WORDS.get(lang, _WORDS["ru"])
    parts = [f"{round(temp):+d}°C"]
    if wind is not None:
        parts.append(wind_fmt.format(w=round(wind)))
    if precip > 0:
        parts.append(precip_fmt.format(p=f"{precip:.1f}".replace(".", sep)))
    return ", ".join(parts)


def summary_for(info: WeatherInfo, lang: Lang = "ru") -> str:
    """WeatherInfo.summary is Russian; the same numbers in kk / en for the answer (T73)."""
    if lang == "ru":
        return info.summary
    return _summary(info.temp_c, info.precipitation_mm, info.wind_ms, lang)
