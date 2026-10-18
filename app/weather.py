"""Current weather from Open-Meteo (no API key). Owner: LLM lane (task T32).

GET https://api.open-meteo.com/v1/forecast?latitude=..&longitude=..
    &current=temperature_2m,precipitation,wind_speed_10m,is_day&wind_speed_unit=ms
- timeout 5 s, cache 15 min per rounded origin (2 decimals) in memory;
- if `at` differs from now by more than 3 hours (demo "time machine") -> return None;
- any failure -> None (the answer then says "погода неизвестна").
outdoor_ok = temp >= -5 and precipitation < 0.5 and wind < 12 (tune later).
"""

from __future__ import annotations

from datetime import datetime

from app.schemas import GeoPoint, WeatherInfo


def get_weather(origin: GeoPoint, at: datetime) -> WeatherInfo | None:
    raise NotImplementedError("T32 (LLM)")
