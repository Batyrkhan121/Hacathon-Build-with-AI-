"""Current weather from Open-Meteo (no API key). Owner: LLM lane (task T32).

GET https://api.open-meteo.com/v1/forecast?latitude=..&longitude=..
    &current=temperature_2m,precipitation,wind_speed_10m,is_day&wind_speed_unit=ms
- total provider budget 2 s, cache 15 min per rounded origin (2 decimals) in memory;
- if `at` differs from now by more than 3 hours (demo "time machine") -> return None;
- any failure -> None (the answer then says "погода неизвестна").
outdoor_ok = temp >= -5 and precipitation < 0.5 and wind < 12 (tune later).

Network: ATTEMPTS tries with a short pause; the coordinates sent are the rounded search origin
(H3 cell centre or landmark), never a raw user point.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import UTC, datetime, timedelta

import httpx

from app.hours import ASTANA_TZ
from app.llm import provider_call
from app.schemas import GeoPoint, Lang, WeatherInfo

log = logging.getLogger(__name__)

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
TIMEOUT_S = 2.0
ATTEMPTS = 1
RETRY_PAUSE_S = 0.5
CACHE_TTL_S = 15 * 60
FAILURE_TTL_S = 60  # while Open-Meteo is down, do not pay its timeout on every request
MAX_SKEW = timedelta(hours=3)
MIN_OUTDOOR_TEMP_C = -5.0
MAX_OUTDOOR_PRECIP_MM = 0.5
MAX_OUTDOOR_WIND_MS = 12.0

_cache: dict[tuple[float, float], tuple[float, WeatherInfo | None]] = {}
_cache_lock = threading.Lock()


def get_weather(origin: GeoPoint, at: datetime) -> WeatherInfo | None:
    key = (round(origin.lat, 2), round(origin.lon, 2))
    try:
        if abs(_as_utc(at) - datetime.now(UTC)) > MAX_SKEW:
            return None  # "time machine" demo: current weather would be misleading
        with _cache_lock:
            hit = _cache.get(key)
        if hit is not None:
            ttl = CACHE_TTL_S if hit[1] is not None else FAILURE_TTL_S
            if time.monotonic() - hit[0] < ttl:
                return hit[1]
        info = _to_info(provider_call(lambda: _fetch(*key), TIMEOUT_S))
    except Exception as exc:  # noqa: BLE001 - weather is optional context
        log.warning("weather unavailable: %s", type(exc).__name__)
        info = None
    with _cache_lock:
        _cache[key] = (time.monotonic(), info)
    return info


def _as_utc(at: datetime) -> datetime:
    if at.tzinfo is None:
        at = at.replace(tzinfo=ASTANA_TZ)
    return at.astimezone(UTC)


def _make_client() -> httpx.Client:
    return httpx.Client(timeout=TIMEOUT_S, headers={"User-Agent": "QadamHackathon/0.1"})


def _fetch(lat: float, lon: float) -> dict:
    params = {
        "latitude": lat,
        "longitude": lon,
        "current": "temperature_2m,precipitation,wind_speed_10m,is_day",
        "wind_speed_unit": "ms",
        "timezone": "UTC",
    }
    last: Exception | None = None
    for attempt in range(1, ATTEMPTS + 1):
        try:
            with _make_client() as client:
                resp = client.get(OPEN_METEO_URL, params=params)
                resp.raise_for_status()
                return resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            last = exc
            if attempt < ATTEMPTS:
                time.sleep(RETRY_PAUSE_S)
    raise RuntimeError("Open-Meteo request failed") from last


def _to_info(data: dict) -> WeatherInfo:
    cur = data["current"]
    temp = float(cur["temperature_2m"])
    precip = float(cur.get("precipitation") or 0.0)
    wind = cur.get("wind_speed_10m")
    wind = None if wind is None else float(wind)
    is_day = cur.get("is_day")
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
        fetched_at=datetime.now(UTC),
    )


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
