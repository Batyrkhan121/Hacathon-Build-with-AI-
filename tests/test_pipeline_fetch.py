"""pipeline.fetch_osm with a fake HTTP transport (no network, no real sleeps)."""

from __future__ import annotations

import os
import time
from pathlib import Path

import httpx
import pytest

from pipeline.fetch_osm import (
    OverpassError,
    area_filters,
    build_query,
    is_fresh,
    run_query,
)


def test_query_has_every_selector_per_center() -> None:
    query = build_query(area_filters("fixture"))
    assert query.startswith("[out:json]")
    assert query.count("around:1500,51.0894,71.4161") == 5
    assert query.count("around:1500,51.0889,71.4103") == 5
    assert query.rstrip().endswith("out center tags;")
    assert "51.03,71.2,51.27,71.65" in build_query(area_filters("astana"))
    with pytest.raises(ValueError):
        area_filters("almaty")


def test_mirror_rotation_and_retry_pauses() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if len(calls) < 4:
            return httpx.Response(429 if len(calls) % 2 else 504)
        return httpx.Response(200, json={"elements": [{"type": "node", "id": 1}]})

    pauses: list[float] = []
    client = httpx.Client(transport=httpx.MockTransport(handler))
    payload = run_query("q", client=client, endpoints=("https://a/i", "https://b/i"),
                        pauses=(5.0, 15.0), sleep=pauses.append)
    assert payload["elements"][0]["id"] == 1
    assert payload["qadam"]["endpoint"] == "https://b/i"
    assert calls == ["https://a/i", "https://b/i", "https://a/i", "https://b/i"]
    assert pauses == [5.0]


def test_all_mirrors_down_raises() -> None:
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    with pytest.raises(OverpassError):
        run_query("q", client=client, endpoints=("https://a/i",), pauses=(1.0,),
                  sleep=lambda s: None)


def test_cache_freshness(tmp_path: Path) -> None:
    path = tmp_path / "osm_fixture.json"
    assert not is_fresh(path)
    path.write_text("{}", encoding="utf-8")
    assert is_fresh(path)
    old = time.time() - 25 * 3600
    os.utime(path, (old, old))
    assert not is_fresh(path)
