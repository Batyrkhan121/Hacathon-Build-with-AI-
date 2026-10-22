"""A copy of eval's test events (eval/fixtures/events_e16.json) moved to another day.

The fixture is dated 24.10.2026, and the UI offers only «Сегодня» and «Завтра»: for a live show
or a backup recording on another day, write a copy with that day's date and start the server
with QADAM_EVENTS_PATH pointing at the copy. Titles keep «(тест)», times, venue and the
example.org source stay as they are; data/events.json is never touched.

Usage:
  python -m eval.shift_events --out <file outside the repo>               # today, Astana time
  python -m eval.shift_events --date 2026-10-25 --out <file>
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any

from app.hours import ASTANA_TZ

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "events_e16.json"


def shift(events: list[dict[str, Any]], day: date) -> list[dict[str, Any]]:
    """Same events on `day`: the date part of starts_at / ends_at changes, nothing else."""
    out = []
    for event in events:
        moved = dict(event)
        for key in ("starts_at", "ends_at"):
            value = event.get(key)
            if value:
                at = datetime.fromisoformat(value)
                moved[key] = at.replace(year=day.year, month=day.month, day=day.day).isoformat()
        out.append(moved)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="eval test events moved to another day")
    ap.add_argument("--date", type=date.fromisoformat, default=None,
                    help="YYYY-MM-DD (default: today in Astana)")
    ap.add_argument("--out", type=Path, required=True, help="where to write the copy")
    args = ap.parse_args(argv)
    day = args.date or datetime.now(ASTANA_TZ).date()
    events = shift(json.loads(FIXTURE.read_text(encoding="utf-8")), day)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(events, ensure_ascii=False, indent=2), encoding="utf-8")

    from app.events import load_events  # the server's own reader: the copy must load

    loaded = load_events(args.out)
    print(f"{len(loaded)} тестовых события на {day:%d.%m.%Y} -> {args.out}")
    print(f"запуск: QADAM_EVENTS_PATH={args.out} .venv/Scripts/python -m uvicorn app.api:app "
          "--port 8000 --no-access-log")
    return 0 if len(loaded) == len(events) else 1


if __name__ == "__main__":
    sys.exit(main())
