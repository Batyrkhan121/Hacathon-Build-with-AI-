"""app/city_gaps.py: the city gaps map file, read and checked once (T142, contract 1.4.0)."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from app import city_gaps

from .conftest import ROOT

MOCK = ROOT / "web" / "mock" / "city_gaps.json"  # valid by the contract test (19 cells)


def test_a_valid_file_is_kept_as_it_is(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    path = tmp_path / "city_gaps.json"
    path.write_bytes(MOCK.read_bytes())
    with caplog.at_level(logging.INFO):
        body = city_gaps.load(path)
    assert body == MOCK.read_bytes()  # served as is, byte for byte
    assert "19 cells" in caplog.text


def test_no_path_or_no_file_gives_none(tmp_path: Path) -> None:
    assert city_gaps.load(None) is None
    assert city_gaps.load(tmp_path / "missing.json") is None


@pytest.mark.parametrize("content", [
    b"",
    b"{not json",
    b"[]",
    json.dumps({"format_version": 1}).encode(),  # fields missing
])
def test_a_broken_file_gives_none(tmp_path: Path, content: bytes) -> None:
    path = tmp_path / "city_gaps.json"
    path.write_bytes(content)
    assert city_gaps.load(path) is None


def test_a_file_failing_the_schema_gives_none_and_logs_no_content(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    data = json.loads(MOCK.read_text(encoding="utf-8"))
    data["city"]["poi"] += 1  # city is no longer the sum over cells
    data["visitor_cells"] = ["8a2153806d1ffff"]  # an extra field: the schema refuses it
    path = tmp_path / "city_gaps.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    with caplog.at_level(logging.DEBUG):
        assert city_gaps.load(path) is None
    assert "ValidationError" in caplog.text
    assert "8a2153806d1ffff" not in caplog.text and "visitor_cells" not in caplog.text


def test_the_repo_file_loads() -> None:
    """data/processed/city_gaps.json (DATA, T141) passes the schema."""
    from app.config import get_settings

    assert city_gaps.load(get_settings().city_gaps_path) is not None
