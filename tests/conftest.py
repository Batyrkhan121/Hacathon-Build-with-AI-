"""Shared pytest fixtures. Owner: INTEGRATOR. Tests never touch the network."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.schemas import POI, Landmark

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).parent / "fixtures"
ASTANA_TZ = timezone(timedelta(hours=5))
EXPO = (51.0894, 71.4161)  # synthetic POIs are placed around this point


def read_jsonl(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


@pytest.fixture(scope="session")
def synthetic_pois() -> list[POI]:
    return [POI.model_validate(row) for row in read_jsonl(FIXTURES / "poi_synthetic.jsonl")]


@pytest.fixture(scope="session")
def landmarks() -> dict[str, Landmark]:
    raw = json.loads((ROOT / "data" / "landmarks.json").read_text(encoding="utf-8"))
    items = [Landmark.model_validate(x) for x in raw]
    return {lm.id: lm for lm in items}


@pytest.fixture
def at_astana() -> Callable[..., datetime]:
    """at_astana(2026, 10, 19, 23, 30) -> tz-aware local datetime (UTC+5)."""

    def _make(year: int, month: int, day: int, hour: int = 12, minute: int = 0) -> datetime:
        return datetime(year, month, day, hour, minute, tzinfo=ASTANA_TZ)

    return _make
