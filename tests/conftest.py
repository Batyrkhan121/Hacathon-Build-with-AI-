"""Shared pytest fixtures. Owner: INTEGRATOR. Tests never touch the network
(except ones marked @pytest.mark.network) and never see a real key."""

from __future__ import annotations

import json
import socket
from collections.abc import Callable, Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.config import get_settings
from app.schemas import POI, Landmark

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).parent / "fixtures"
ASTANA_TZ = timezone(timedelta(hours=5))
EXPO = (51.0894, 71.4161)  # synthetic POIs are placed around this point

# Values a developer's .env may hold; every test gets these instead. Set, not deleted: a later
# load_dotenv() never overrides a variable that exists, even an empty one.
TEST_ENV = {
    "ANTHROPIC_API_KEY": "test-key-not-real",  # has_llm stays true; a real call hits _no_network
    "GEMINI_API_KEY": "",
    "OPENROUTER_API_KEY": "",
    "OPEN_ROUTER_KEY": "",  # read by app.config as a fallback for OPENROUTER_API_KEY
    "EMBED_PROVIDER": "gemini",  # stub provider: no Gemini key -> no embeddings
    "LLM_ENABLED": "true",
    "WALK_GRAPH": "false",  # straight segments unless a test sets a graph itself
}


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "network: the test may connect outside loopback")


@pytest.fixture(autouse=True)
def _no_live_keys(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Same settings with or without a .env, so no test can spend money or quota. A test that
    needs an embeddings key sets a fake one itself."""
    for name, value in TEST_ENV.items():
        monkeypatch.setenv(name, value)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _is_loopback(address: object) -> bool:
    host = address[0] if isinstance(address, tuple) else address
    return isinstance(host, str) and (host.startswith("127.") or host in ("::1", "localhost"))


@pytest.fixture(autouse=True)
def _no_network(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
                ) -> Iterator[None]:
    """A connect outside loopback is refused like a dead network (so code takes its fallback)
    and fails the test at teardown, even when the code swallowed the error.
    @pytest.mark.network lifts the ban (keys stay empty all the same)."""
    if request.node.get_closest_marker("network"):
        yield
        return
    attempts: list[str] = []
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex

    def refuse(address: object) -> None:
        attempts.append(repr(address))
        raise ConnectionRefusedError(f"tests never touch the network: {address!r}")

    def connect(self: socket.socket, address: object) -> None:
        if not _is_loopback(address):
            refuse(address)
        real_connect(self, address)

    def connect_ex(self: socket.socket, address: object) -> int:
        if not _is_loopback(address):
            refuse(address)
        return real_connect_ex(self, address)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
    yield
    if attempts:
        pytest.fail(f"test tried to reach the network: {', '.join(attempts)}")


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
