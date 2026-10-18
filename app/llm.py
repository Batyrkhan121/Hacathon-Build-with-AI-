"""Claude client wrapper (anthropic SDK) + optional Gemini embeddings. Owner: LLM lane (T30).

Requirements:
- client: `anthropic.Anthropic(api_key=settings.anthropic_api_key,
  timeout=settings.llm_timeout_s, max_retries=2, default_headers=settings.anthropic_headers)`
  (the header carries ANTHROPIC_WORKSPACE_ID for keys not scoped to a workspace);
- structured output into Pydantic models:
  `client.messages.parse(model=..., max_tokens=..., system=..., messages=[...],
  output_format=Schema, output_config={"effort": effort})` -> `resp.parsed_output`;
  check `resp.stop_reason` first ("refusal" / "max_tokens" -> LLMUnavailable);
- models: settings.claude_model (claude-sonnet-5-5), then settings.claude_fallback_model
  (claude-haiku-5-5). Sonnet 5.5 rejects non-default temperature/top_p (400) — control with
  `effort` only; thinking is adaptive by default, keep effort "low" for latency;
- refusal fallback on the Claude API: `client.beta.messages.parse(...,
  betas=["server-side-fallback-2026-07-01"], fallbacks="default")`;
- timeout settings.llm_timeout_s, SDK retries (max_retries=2), then the fallback model,
  then LLMUnavailable;
- disk cache data/cache/llm/<sha256>.json keyed by (model, system, prompt, schema name)
  so preset demo scenarios are instant and survive quota problems;
- embeddings (Anthropic has no embeddings API): Gemini via google-genai, only when
  settings.has_embeddings; settings.embed_model / settings.embed_dim, L2-normalised,
  task_type RETRIEVAL_QUERY for queries and RETRIEVAL_DOCUMENT for documents
  (pipeline/build_index.py must produce document vectors the same way);
- never log the API keys or full prompts with user location.
Verify the SDK call shapes on the installed versions with scripts/check_llm.py first.
"""

from __future__ import annotations

from typing import Literal, TypeVar

import numpy as np
from pydantic import BaseModel

from app.config import Settings

T = TypeVar("T", bound=BaseModel)
Effort = Literal["low", "medium", "high"]


class LLMUnavailable(RuntimeError):
    """Raised when the LLM cannot answer (no key, quota, timeout, refusal, invalid output)."""


class LLMClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.model_name = settings.claude_model

    @property
    def available(self) -> bool:
        return self.settings.has_llm

    def generate_structured(
        self, *, system: str, prompt: str, schema: type[T], effort: Effort = "low"
    ) -> T:
        """Return a validated `schema` instance or raise LLMUnavailable."""
        raise NotImplementedError("T30 (LLM)")

    def embed_query(self, text: str) -> np.ndarray | None:
        """L2-normalised query vector, or None without GEMINI_API_KEY / on any failure
        (retrieval goes lexical)."""
        raise NotImplementedError("T30 (LLM)")

    def embed_documents(self, texts: list[str]) -> np.ndarray:
        """(len(texts), embed_dim) float32, L2-normalised. Batches internally.
        Raise LLMUnavailable without GEMINI_API_KEY (build_index then skips the index)."""
        raise NotImplementedError("T30 (LLM)")
