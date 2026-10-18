"""Runtime settings from environment / .env. Owner: INTEGRATOR.

LLM (query parsing, card texts, POI descriptions) = Claude via the `anthropic` SDK.
Embeddings are optional: Gemini via `google-genai` only when GEMINI_API_KEY is set
(Anthropic has no embeddings API); without it retrieval is lexical.
Never print or log `anthropic_api_key` / `gemini_api_key`.
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
    anthropic_api_key: str | None
    claude_model: str
    claude_fallback_model: str
    gemini_api_key: str | None  # optional, embeddings only
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
        return self.llm_enabled and bool(self.anthropic_api_key)

    @property
    def has_embeddings(self) -> bool:
        return bool(self.gemini_api_key)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    data_dir = Path(os.getenv("QADAM_DATA_DIR", ROOT / "data"))
    return Settings(
        anthropic_api_key=os.getenv("ANTHROPIC_API_KEY") or None,
        claude_model=os.getenv("CLAUDE_MODEL") or "claude-sonnet-5-5",
        claude_fallback_model=os.getenv("CLAUDE_FALLBACK_MODEL") or "claude-haiku-5-5",
        gemini_api_key=os.getenv("GEMINI_API_KEY") or None,
        embed_model=os.getenv("EMBED_MODEL") or "gemini-embedding-001",
        embed_dim=int(os.getenv("EMBED_DIM") or "768"),
        llm_enabled=_bool("LLM_ENABLED", True),
        llm_timeout_s=float(os.getenv("LLM_TIMEOUT_S") or "20"),
        h3_res=int(os.getenv("H3_RES") or "9"),
        data_dir=data_dir,
        poi_path=Path(os.getenv("QADAM_POI_PATH", data_dir / "processed" / "poi.jsonl")),
        index_dir=Path(os.getenv("QADAM_INDEX_DIR", data_dir / "index")),
        landmarks_path=Path(os.getenv("QADAM_LANDMARKS_PATH", data_dir / "landmarks.json")),
        cache_dir=Path(os.getenv("QADAM_CACHE_DIR", data_dir / "cache")),
        log_level=os.getenv("LOG_LEVEL", "INFO"),
        contact=os.getenv("QADAM_CONTACT") or "team@example.com",
    )
