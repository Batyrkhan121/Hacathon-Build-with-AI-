"""Gemini client wrapper (google-genai SDK). Owner: LLM lane (task T30).

Requirements:
- structured output into Pydantic models (response_mime_type="application/json" + schema);
- timeout settings.llm_timeout_s, 2 retries with backoff, then settings.gemini_fallback_model;
- disk cache data/cache/llm/<sha256>.json keyed by (model, system, prompt, schema name)
  so preset demo scenarios are instant and survive quota problems;
- embeddings with settings.embed_model / settings.embed_dim, L2-normalised,
  task_type RETRIEVAL_QUERY for queries and RETRIEVAL_DOCUMENT for documents
  (pipeline/build_index.py must produce document vectors the same way);
- never log the API key or full prompts with user location.
Verify current SDK call shapes with scripts/check_gemini.py before relying on them.
"""

from __future__ import annotations

from typing import TypeVar

import numpy as np
from pydantic import BaseModel

from app.config import Settings

T = TypeVar("T", bound=BaseModel)


class LLMUnavailable(RuntimeError):
    """Raised when the LLM cannot answer (no key, quota, timeout, invalid output)."""


class LLMClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.model_name = settings.gemini_model

    @property
    def available(self) -> bool:
        return self.settings.has_llm

    def generate_structured(
        self, *, system: str, prompt: str, schema: type[T], temperature: float = 0.3
    ) -> T:
        """Return a validated `schema` instance or raise LLMUnavailable."""
        raise NotImplementedError("T30 (LLM)")

    def embed_query(self, text: str) -> np.ndarray | None:
        """L2-normalised query vector, or None on any failure (retrieval goes lexical)."""
        raise NotImplementedError("T30 (LLM)")

    def embed_documents(self, texts: list[str]) -> np.ndarray:
        """(len(texts), embed_dim) float32, L2-normalised. Batches internally."""
        raise NotImplementedError("T30 (LLM)")
