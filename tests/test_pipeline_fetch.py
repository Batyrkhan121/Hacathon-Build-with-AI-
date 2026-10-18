"""pipeline.fetch_osm with a fake HTTP transport (no network, no real sleeps)."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import httpx
import pytest

from pipeline.fetch_osm import (
    ASTANA_BBOX,
    OverpassError,
    area_chunks,
    bbox_tiles,
    build_query,
    fetch_area,
    is_fresh,
    mirror_state,
    prefer,
    run_query,
    status_url,
)


def test_query_has_every_selector_per_center() -> None:
    (chunk,) = area_chunks("fixture")
    query = build_query(chunk.filters)
    assert query.startswith("[out:json]")
    assert query.count("around:1500,51.0894,71.4161") == 5
    assert query.count("around:1500,51.0889,71.4103") == 5
    assert query.rstrip().endswith("out center tags;")
    with pytest.raises(ValueError):
        area_chunks("almaty")


def test_zones_and_tiles() -> None:
    zones = area_chunks("zones")
    assert len(zones) == 5 and all(c.name.startswith("zone_") for c in zones)
    assert all(f.startswith("around:2000,") for c in zones for f in c.filters)
    tiles = area_chunks("astana")
    assert 6 <= len(tiles) <= 9
    assert len({c.name for c in tiles}) == len(tiles)
    boxes = bbox_tiles(ASTANA_BBOX, 3, 3)
    assert boxes[0][:2] == ASTANA_BBOX[:2] and boxes[-1][2:] == ASTANA_BBOX[2:]


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


def test_status_check_skips_busy_mirror() -> None:
    posted: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/status"):
            if "busy" in url:
                return httpx.Response(200, text="Rate limit: 2\nSlot available after: x")
            return httpx.Response(200, text="Rate limit: 2\n2 slots available now.")
        posted.append(url)
        return httpx.Response(200, json={"elements": []})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    payload = run_query("q", client=client, check_status=True, sleep=lambda s: None,
                        endpoints=("https://busy/api/interpreter", "https://ok/api/interpreter"))
    assert payload["qadam"]["endpoint"] == "https://ok/api/interpreter"
    assert posted == ["https://ok/api/interpreter"]


def test_mirror_state_values() -> None:
    def client_for(resp: httpx.Response) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(lambda r: resp))

    assert status_url("https://x/api/interpreter") == "https://x/api/status"
    assert mirror_state(client_for(httpx.Response(404)), "https://x/api/interpreter") == "unknown"
    assert mirror_state(client_for(httpx.Response(504)), "https://x/api/interpreter") == "unknown"

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("no route", request=request)

    down = httpx.Client(transport=httpx.MockTransport(refuse))
    assert mirror_state(down, "https://x/api/interpreter") == "down"


def test_prefer_last_good_mirror() -> None:
    assert prefer("b", ["a", "b", "c"]) == ["b", "a", "c"]
    assert prefer(None, ["a", "b"]) == ["a", "b"]
    assert prefer("z", ["a", "b"]) == ["a", "b"]


def test_fetch_area_resumes_and_reports_failures(tmp_path: Path) -> None:
    chunks = area_chunks("zones")
    cached = tmp_path / f"osm_{chunks[0].name}.json"
    cached.write_text('{"elements": []}', encoding="utf-8")
    calls: list[str] = []

    def fake_query(query: str, **kwargs: object) -> dict:
        calls.append(query)
        if len(calls) == 2:
            raise OverpassError("down")
        return {"elements": [{"type": "node", "id": len(calls)}], "qadam": {"endpoint": "e"}}

    res = fetch_area("zones", raw_dir=tmp_path, query_fn=fake_query, sleep=lambda s: None)
    assert res.cached == [chunks[0].name]
    assert len(calls) == len(chunks) - 1
    assert len(res.failed) == 1 and res.failed[0].startswith(chunks[2].name)
    saved = json.loads((tmp_path / f"osm_{chunks[1].name}.json").read_text(encoding="utf-8"))
    assert saved["qadam"]["chunk"] == chunks[1].name and saved["qadam"]["fetched_at_unix"]

    # rerun: only the failed chunk is requested again
    calls.clear()
    res2 = fetch_area("zones", raw_dir=tmp_path, query_fn=fake_query, sleep=lambda s: None)
    assert len(calls) == 1 and not res2.failed


def test_fetch_area_deadline(tmp_path: Path) -> None:
    res = fetch_area("zones", raw_dir=tmp_path, deadline=time.time() - 1,
                     query_fn=lambda *a, **k: pytest.fail("no request after deadline"))
    assert len(res.failed) == 5 and all(f.endswith("deadline") for f in res.failed)


def test_cache_freshness(tmp_path: Path) -> None:
    path = tmp_path / "osm_fixture.json"
    assert not is_fresh(path)
    path.write_text("{}", encoding="utf-8")
    assert is_fresh(path)
    old = time.time() - 25 * 3600
    os.utime(path, (old, old))
    assert not is_fresh(path)


def test_fetch_until_retries_missing_chunks(tmp_path: Path) -> None:
    from pipeline.fetch_osm import fetch_until

    calls: list[str] = []

    def flaky(query: str, **kwargs: object) -> dict:
        calls.append(query)
        if len(calls) <= 2:
            raise OverpassError("down")
        return {"elements": [], "qadam": {"endpoint": "e"}}

    pauses: list[float] = []
    res = fetch_until("zones", time.time() + 3600, raw_dir=tmp_path, query_fn=flaky,
                      sleep=pauses.append, round_pause_s=60.0)
    assert not res.failed
    assert len(calls) == 5 + 2  # two failures in round 1, refetched in round 2
    assert 60.0 in pauses
