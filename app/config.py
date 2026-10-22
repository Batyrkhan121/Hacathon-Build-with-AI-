"""Runtime settings from environment / .env. Owner: INTEGRATOR.

LLM (query parsing, card texts, POI descriptions) = Claude via the `anthropic` SDK.
Embeddings are optional (Anthropic has no embeddings API); without them retrieval is lexical.
EMBED_PROVIDER picks where they come from:
- "openrouter" (default, data/index is built this way): Gemini's model through OpenRouter's
  OpenAI-compatible /api/v1/embeddings, needs OPENROUTER_API_KEY (OPEN_ROUTER_KEY is read as
  a fallback). The model id gets the vendor prefix ("google/gemini-embedding-001"), so its
  vectors and cache never mix with an index built through Gemini directly (meta.model must match);
- "gemini" (fallback): Gemini via `google-genai`, needs GEMINI_API_KEY and an index built with it.
WALK_GRAPH (default on): routes follow the OSM walk graph data/processed/walk_graph.npz when the
file exists (`walk_graph_ready`); off, missing or any error -> straight segments.
QADAM_EVENTS_PATH (default data/events.json): the dated events file; eval points it at its own
fixture so a test event never goes into data/.
QADAM_CITY_GAPS_PATH (default data/processed/city_gaps.json): the city gaps map (CityGaps) that
GET /api/city-gaps serves; tests point it at their own file.
Public demo (final package, T161): QADAM_RATE_PER_MIN / QADAM_RATE_PER_DAY limit POST
/api/recommend per client IP (0 = off, the default; the image turns them on);
QADAM_TRUSTED_PROXY_HOPS = proxies in front that append to X-Forwarded-For (Render: 1).
LLM_DAILY_BUDGET_USD caps Claude spend per Astana day inside the app (unset = no cap here; the
Console workspace limit stays the backstop); LLM_PRICE_IN_PER_MTOK / LLM_PRICE_OUT_PER_MTOK are
USD per million tokens from the price page. A budget without both prices = no Claude at all.
Never print or log `anthropic_api_key` / `gemini_api_key` / `openrouter_api_key`.
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


def _int(name: str, default: int) -> int:
    raw = (os.getenv(name) or "").strip()
    value = int(raw) if raw else default
    if value < 0:
        raise ValueError(f"{name} must be >= 0")
    return value


def _float(name: str) -> float | None:
    raw = (os.getenv(name) or "").strip()
    return float(raw) if raw else None


@dataclass(frozen=True)
class Settings:
    anthropic_api_key: str | None
    anthropic_workspace_id: str | None  # needed only for a user-level key not scoped to one
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
    openrouter_api_key: str | None = None  # optional, embeddings through OpenRouter only
    embed_provider: str = "openrouter"  # "openrouter" | "gemini"
    walk_graph_enabled: bool = True  # WALK_GRAPH; false -> straight segments
    walk_graph_path: Path | None = None  # data/processed/walk_graph.npz (QADAM_WALK_GRAPH_PATH)
    # data/events.json (QADAM_EVENTS_PATH); eval points it at its own fixture, data stays as is
    events_path: Path | None = None
    # data/processed/city_gaps.json (QADAM_CITY_GAPS_PATH): aggregates of open data, wave 9
    city_gaps_path: Path | None = None
    # public demo (T161): per-IP limits of POST /api/recommend, 0 = off
    rate_per_min: int = 0
    rate_per_day: int = 0
    trusted_proxy_hops: int = 0
    # daily Claude spend cap (USD per Astana day) and prices (USD per million tokens)
    llm_daily_budget_usd: float | None = None
    llm_price_in_per_mtok: float | None = None
    llm_price_out_per_mtok: float | None = None

    @property
    def llm_budget_state(self) -> str:
        """"none" — no cap in the app; "set" — budget and both prices; "misconfigured" — a
        budget without usable prices: then no Claude calls (fail safe)."""
        if self.llm_daily_budget_usd is None:
            return "none"
        prices = (self.llm_price_in_per_mtok, self.llm_price_out_per_mtok)
        if self.llm_daily_budget_usd < 0 or any(p is None or p <= 0 for p in prices):
            return "misconfigured"
        return "set"

    @property
    def has_llm(self) -> bool:
        return self.llm_enabled and bool(self.anthropic_api_key)

    @property
    def anthropic_headers(self) -> dict[str, str]:
        """For anthropic.Anthropic(default_headers=...)."""
        if not self.anthropic_workspace_id:
            return {}
        return {"anthropic-workspace-id": self.anthropic_workspace_id}

    @property
    def walk_graph_meta_path(self) -> Path | None:
        if self.walk_graph_path is None:
            return None
        return self.walk_graph_path.with_name("walk_graph_meta.json")

    @property
    def walk_graph_ready(self) -> bool:
        """WALK_GRAPH is on and the graph file exists (load errors: app.walkgraph falls back)."""
        path = self.walk_graph_path
        return bool(self.walk_graph_enabled and path is not None and path.exists())

    @property
    def has_embeddings(self) -> bool:
        if self.embed_provider == "openrouter":
            return bool(self.openrouter_api_key)
        return bool(self.gemini_api_key)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    data_dir = Path(os.getenv("QADAM_DATA_DIR", ROOT / "data"))
    provider = (os.getenv("EMBED_PROVIDER") or "openrouter").strip().lower()
    if provider not in ("gemini", "openrouter"):
        raise ValueError(f"EMBED_PROVIDER must be 'gemini' or 'openrouter', got {provider!r}")
    embed_model = os.getenv("EMBED_MODEL") or "gemini-embedding-001"
    if provider == "openrouter" and "/" not in embed_model:
        embed_model = f"google/{embed_model}"  # OpenRouter ids carry the vendor prefix
    return Settings(
        anthropic_api_key=os.getenv("ANTHROPIC_API_KEY") or None,
        anthropic_workspace_id=os.getenv("ANTHROPIC_WORKSPACE_ID") or None,
        claude_model=os.getenv("CLAUDE_MODEL") or "claude-sonnet-5-5",
        claude_fallback_model=os.getenv("CLAUDE_FALLBACK_MODEL") or "claude-haiku-5-5",
        gemini_api_key=os.getenv("GEMINI_API_KEY") or None,
        embed_model=embed_model,
        embed_dim=int(os.getenv("EMBED_DIM") or "768"),
        llm_enabled=_bool("LLM_ENABLED", True),
        llm_timeout_s=float(os.getenv("LLM_TIMEOUT_S") or "20"),
        h3_res=int(os.getenv("H3_RES") or "10"),
        data_dir=data_dir,
        poi_path=Path(os.getenv("QADAM_POI_PATH", data_dir / "processed" / "poi.jsonl")),
        index_dir=Path(os.getenv("QADAM_INDEX_DIR", data_dir / "index")),
        landmarks_path=Path(os.getenv("QADAM_LANDMARKS_PATH", data_dir / "landmarks.json")),
        cache_dir=Path(os.getenv("QADAM_CACHE_DIR", data_dir / "cache")),
        log_level=os.getenv("LOG_LEVEL", "INFO"),
        contact=os.getenv("QADAM_CONTACT") or "team@example.com",
        openrouter_api_key=(os.getenv("OPENROUTER_API_KEY") or os.getenv("OPEN_ROUTER_KEY")
                            or None),
        embed_provider=provider,
        walk_graph_enabled=_bool("WALK_GRAPH", True),
        walk_graph_path=Path(os.getenv("QADAM_WALK_GRAPH_PATH",
                                       data_dir / "processed" / "walk_graph.npz")),
        events_path=Path(os.getenv("QADAM_EVENTS_PATH", data_dir / "events.json")),
        city_gaps_path=Path(os.getenv("QADAM_CITY_GAPS_PATH",
                                      data_dir / "processed" / "city_gaps.json")),
        rate_per_min=_int("QADAM_RATE_PER_MIN", 0),
        rate_per_day=_int("QADAM_RATE_PER_DAY", 0),
        trusted_proxy_hops=_int("QADAM_TRUSTED_PROXY_HOPS", 0),
        llm_daily_budget_usd=_float("LLM_DAILY_BUDGET_USD"),
        llm_price_in_per_mtok=_float("LLM_PRICE_IN_PER_MTOK"),
        llm_price_out_per_mtok=_float("LLM_PRICE_OUT_PER_MTOK"),
    )
