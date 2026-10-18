"""Runtime settings from environment / .env. Owner: INTEGRATOR.

Never print or log `gemini_api_key`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    gemini_api_key: str | None
    gemini_model: str
    gemini_fallback_model: str
    embed_model: str
    embed_dim: int
    llm_enabled: bool
    llm_timeout_s: float
    h3_res: int
    data_dir: Path
    poi_path: Path
    index_dir: Path
    landmarks_path: Path
    cache_dir: Path
    log_level: str
    contact: str  # QADAM_CONTACT, goes into the User-Agent of Overpass / Wikidata requests

    @property
    def has_llm(self) -> bool:
        return self.llm_enabled and bool(self.gemini_api_key)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    data_dir = Path(os.getenv("QADAM_DATA_DIR", ROOT / "data"))
    return Settings(
        gemini_api_key=os.getenv("GEMINI_API_KEY") or None,
        gemini_model=os.getenv("GEMINI_MODEL", "gemini-3.8-flash"),
        gemini_fallback_model=os.getenv("GEMINI_FALLBACK_MODEL", "gemini-3.5-flash-lite"),
        embed_model=os.getenv("EMBED_MODEL", "gemini-embedding-001"),
        embed_dim=int(os.getenv("EMBED_DIM", "768")),
        llm_enabled=_bool("LLM_ENABLED", True),
        llm_timeout_s=float(os.getenv("LLM_TIMEOUT_S", "20")),
        h3_res=int(os.getenv("H3_RES", "9")),
        data_dir=data_dir,
        poi_path=Path(os.getenv("QADAM_POI_PATH", data_dir / "processed" / "poi.jsonl")),
        index_dir=Path(os.getenv("QADAM_INDEX_DIR", data_dir / "index")),
        landmarks_path=Path(os.getenv("QADAM_LANDMARKS_PATH", data_dir / "landmarks.json")),
        cache_dir=Path(os.getenv("QADAM_CACHE_DIR", data_dir / "cache")),
        log_level=os.getenv("LOG_LEVEL", "INFO"),
        contact=os.getenv("QADAM_CONTACT") or "team@example.com",
    )
