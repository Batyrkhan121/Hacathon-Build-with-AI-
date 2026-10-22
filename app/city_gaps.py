"""«Карта пробелов данных»: data/processed/city_gaps.json for GET /api/city-gaps.

Owner: FEATURES lane (T142, wave 9). Contract 1.4.0: docs/ARCHITECTURE.md §5, `CityGaps` in
app/schemas.py. The file is built by DATA (pipeline/city_gaps.py) from open OSM data only.

- load(path): read once at server start (api lifespan) and checked with `CityGaps`; the server
  never recomputes it. The bytes are kept as they are in the file and served as is to everyone
  (the endpoint has no parameters: one answer for the whole city).
- No file, an unreadable file or one that fails the schema -> None: the endpoint answers 503
  {"detail": "city gaps unavailable"}, /api/health says "missing", the rest of the API works.
  The log line names only the kind of problem, never the file content.
"""

from __future__ import annotations

import logging
from pathlib import Path

from pydantic import ValidationError

from app.schemas import CityGaps

log = logging.getLogger(__name__)

UNAVAILABLE = "city gaps unavailable"  # the 503 detail of the contract: no details on purpose
CACHE_CONTROL = "public, max-age=3600"  # the same answer for everyone, rebuilt with the data


def load(path: Path | None) -> bytes | None:
    """The file's bytes when it passes the CityGaps schema, else None (quietly: a warning with
    the reason kind only)."""
    if path is None:
        log.warning("city gaps map: no path configured")
        return None
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        log.warning("city gaps map: no file, GET /api/city-gaps answers 503")
        return None
    except OSError as exc:
        log.warning("city gaps map: file unreadable (%s), answers 503", type(exc).__name__)
        return None
    try:
        gaps = CityGaps.model_validate_json(raw)
    except (ValidationError, ValueError) as exc:
        log.warning("city gaps map: file does not pass the schema (%s), answers 503",
                    type(exc).__name__)
        return None
    log.info("city gaps map loaded: %d cells", len(gaps.cells))
    return raw
