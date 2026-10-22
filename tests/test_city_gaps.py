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


# --------------------------- T152: only a file exactly as the schema writes it (strict check)


def _mock() -> dict:
    return json.loads(MOCK.read_text(encoding="utf-8"))


def _edit_count(spell):  # type: ignore[no-untyped-def]
    def edit(d: dict) -> None:  # the same number, only spelled otherwise: every sum still holds
        d["cells"][0]["walk_m"] = spell(d["cells"][0]["walk_m"])
    return edit


HAND_EDITS = {
    "count as a string": _edit_count(str),
    "count as 120.0": _edit_count(float),
    "built_at as a number": lambda d: d.__setitem__("built_at", 1761120000),
    "built_at in another spelling": lambda d: d.__setitem__(
        "built_at", d["built_at"].replace("+05:00", ".000+05:00")),
    "no attribution": lambda d: d.pop("attribution"),
    "no license_url": lambda d: d.pop("license_url"),
    "no h3_res": lambda d: d.pop("h3_res"),
    "no format_version": lambda d: d.pop("format_version"),
    "no boundaries_source": lambda d: d["districts"].pop("boundaries_source"),
}


@pytest.mark.parametrize("edit", list(HAND_EDITS.values()), ids=list(HAND_EDITS))
def test_a_hand_edited_file_that_only_the_lax_reading_takes_is_refused(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, edit
) -> None:
    from app.schemas import CityGaps

    data = _mock()
    edit(data)
    raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
    CityGaps.model_validate_json(raw)  # the lax reading would take it…
    path = tmp_path / "city_gaps.json"
    path.write_bytes(raw)
    with caplog.at_level(logging.DEBUG):
        assert city_gaps.load(path) is None  # …but the served bytes must be the schema's
    assert "answers 503" in caplog.text
    assert data["cells"][0]["h3_cell"] not in caplog.text  # the kind only, no content


@pytest.mark.parametrize("path", [MOCK, ROOT / "data" / "processed" / "city_gaps.json"],
                         ids=["mock-with-districts", "real-data"])
def test_the_files_as_the_schema_writes_them_are_kept(path: Path) -> None:
    """The mock with three districts (ok, stale, missing: optional fields left out) and the
    real data file (no districts block yet) pass all the checks."""
    assert city_gaps.load(path) == path.read_bytes()


def test_a_file_rewritten_from_its_own_dump_stays_good(tmp_path: Path) -> None:
    from app.schemas import CityGaps

    gaps = CityGaps.model_validate_json(MOCK.read_bytes())
    path = tmp_path / "city_gaps.json"
    path.write_text(gaps.model_dump_json(exclude_unset=True), encoding="utf-8")
    assert city_gaps.load(path) is not None
    path.write_text(gaps.model_dump_json(), encoding="utf-8")  # optional fields written out
    assert city_gaps.load(path) is not None
