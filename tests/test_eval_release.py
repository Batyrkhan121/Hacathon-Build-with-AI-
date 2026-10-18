"""The release gate must stop on failing scenarios, not just print FAIL."""

import json
from pathlib import Path

from eval import run_eval


def test_strict_eval_reports_failure(tmp_path: Path) -> None:
    scenarios = tmp_path / "scenarios.yaml"
    scenarios.write_text(json.dumps([{
        "id": "FAIL", "title": "Deliberately failing gate",
        "request": {"query": "coffee", "location": {"landmark_id": "expo"}},
        "expect": {"max_cards": 0},
    }]), encoding="utf-8")
    assert run_eval.main([
        "--mock", "--strict", "--scenarios", str(scenarios), "--out", str(tmp_path),
    ]) == 1
