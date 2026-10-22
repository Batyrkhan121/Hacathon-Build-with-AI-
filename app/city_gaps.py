"""«Карта пробелов данных»: data/processed/city_gaps.json for GET /api/city-gaps.

Owner: FEATURES lane (T142, wave 9; T152, wave 9.1). Contract 1.4.1: docs/ARCHITECTURE.md §5,
`CityGaps` in app/schemas.py. The file is built by DATA (pipeline/city_gaps.py) from open OSM
data only; districts (1.4.1) are passed through as they are.

- load(path): read once at server start (api lifespan); the server never recomputes it. The
  bytes are kept as they are in the file and served as is to everyone (the endpoint has no
  parameters: one answer for the whole city).
- Because the raw bytes are served, the file is accepted only when it is exactly what the
  schema writes (T152): `CityGaps` in strict mode (no "5" or 5.0 for a count, no number for
  built_at); every field with a constant default (format_version, h3_res, the attribution
  fields, boundaries_source) present in the file; and the file equal to its own
  model_dump(mode="json") — optional fields (a cell's district, the districts block, a missing
  district's outline…) may be left out. A file edited by hand that only the lax reading would
  take is refused.
- No file, an unreadable file or one that fails any check -> None: the endpoint answers 503
  {"detail": "city gaps unavailable"}, /api/health says "missing", the rest of the API works.
  The log line names only the kind of problem, never the file content.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from pydantic import BaseModel, ValidationError
from pydantic_core import PydanticUndefined

from app.schemas import CityGaps

log = logging.getLogger(__name__)

UNAVAILABLE = "city gaps unavailable"  # the 503 detail of the contract: no details on purpose
CACHE_CONTROL = "public, max-age=3600"  # the same answer for everyone, rebuilt with the data


def load(path: Path | None) -> bytes | None:
    """The file's bytes when it is exactly a CityGaps as the schema writes it, else None
    (quietly: a warning with the kind of problem only)."""
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
        gaps = CityGaps.model_validate_json(raw, strict=True)
        written = json.loads(raw)
    except (ValidationError, ValueError) as exc:
        log.warning("city gaps map: file does not pass the strict schema (%s), answers 503",
                    type(exc).__name__)
        return None
    if _defaults_left_out(gaps):
        log.warning("city gaps map: file leaves out fields of the contract, answers 503")
        return None
    if written != gaps.model_dump(mode="json", exclude_unset=True):
        log.warning("city gaps map: file is not written as the schema writes it, answers 503")
        return None
    log.info("city gaps map loaded: %d cells", len(gaps.cells))
    return raw


def _defaults_left_out(model: BaseModel) -> bool:
    """A field with a constant default (not None, not a factory) missing in the file, at any
    depth: the lax reading would fill it in, but the served bytes would not have it."""
    for name, field in type(model).model_fields.items():
        if (name not in model.model_fields_set and field.default is not PydanticUndefined
                and field.default is not None and field.default_factory is None):
            return True
        value = getattr(model, name)
        for child in value if isinstance(value, list) else [value]:
            if isinstance(child, BaseModel) and _defaults_left_out(child):
                return True
    return False
