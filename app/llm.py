"""Claude client wrapper (anthropic SDK) + optional Gemini embeddings. Owner: LLM lane (T30).

Requirements:
- client: `anthropic.Anthropic(api_key=settings.anthropic_api_key,
  timeout=settings.llm_timeout_s, max_retries=0, default_headers=settings.anthropic_headers)`
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
- one shared request_budget (20 s default); primary and fallback divide remaining time,
  with no online SDK retries. A bounded worker pool enforces wall time even if an SDK stalls;
- disk cache data/cache/llm/<sha256>.json keyed by (model, system, prompt, schema name)
  so preset demo scenarios are instant and survive quota problems;
- embeddings (Anthropic has no embeddings API): Gemini via google-genai, only when
  settings.has_embeddings; settings.embed_model / settings.embed_dim, L2-normalised,
  task_type RETRIEVAL_QUERY for queries and RETRIEVAL_DOCUMENT for documents
  (pipeline/build_index.py must produce document vectors the same way);
- never log the API keys or full prompts with user location.
Verify the SDK call shapes on the installed versions with scripts/check_llm.py first.

Verified on anthropic 1.15.0 / google-genai 2.31.0 (scripts/check_llm.py):
- `beta.messages.parse(..., fallbacks="default")` works on both models; on a model without
  server-side fallback the request simply runs alone, so one call shape serves both.
- `parse()` validates the JSON inside the SDK: truncated or refused text raises
  pydantic.ValidationError instead of returning parsed_output=None — both are handled.
- The first request with a never-seen schema can exceed 20 s (schema compilation, then cached
  server-side); run scripts/check_llm.py before a demo to warm it up.
Failure policy: auth/permission errors or "every model failed on network/timeouts" put the
client into a cooldown, so the demo answers instantly from the deterministic path instead of
waiting for timeouts on every request.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal, TypeVar

import numpy as np
from pydantic import BaseModel, ValidationError

from app.config import Settings

log = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)
R = TypeVar("R")
Effort = Literal["low", "medium", "high"]

FALLBACK_BETA = "server-side-fallback-2026-07-01"
MAX_TOKENS = 8192  # adaptive thinking shares this budget; cards need ~500 output tokens
SDK_RETRIES = 0  # online requests share one wall-clock deadline; no hidden SDK retries
FALLBACK_RETRIES = 0
REQUEST_BUDGET_S = 20.0
MAX_PROVIDER_WORKERS = 8
QUERY_EMBED_TIMEOUT_S = 2.0
_request_state = threading.local()
_provider_slots = threading.BoundedSemaphore(MAX_PROVIDER_WORKERS)
NETWORK_COOLDOWN_S = 60.0
AUTH_COOLDOWN_S = 300.0
CACHE_VERSION = 1
REDACTED_FIELDS = ("raw_query",)  # never written to the disk cache (callers re-fill them)

EMBED_BATCH = 100
EMBED_ATTEMPTS = 3
EMBED_PAUSE_S = 1.0  # doubled after every failed attempt
EMBED_TIMEOUT_MS = 15_000

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"
_USER_TAG_RE = re.compile(r"<\s*/?\s*user_query\s*>", re.IGNORECASE)


@lru_cache(maxsize=16)
def load_prompt(name: str) -> str:
    """Text of prompts/<name>.md (utf-8)."""
    return (PROMPTS_DIR / f"{name}.md").read_text(encoding="utf-8")


def user_data_block(text: str) -> str:
    """The user's text as a delimited DATA block for a prompt.

    Tags that could close the block early are removed, so the text cannot "escape" it.
    """
    clean = _USER_TAG_RE.sub(" ", text).strip()
    return f"<user_query>\n{clean}\n</user_query>"


class LLMUnavailable(RuntimeError):
    """Raised when the LLM cannot answer (no key, quota, timeout, refusal, invalid output)."""


class ProviderTimeout(LLMUnavailable):
    """The shared request deadline or an individual provider deadline expired."""


@contextmanager
def request_budget(timeout_s: float = REQUEST_BUDGET_S) -> Iterator[None]:
    """Share a monotonic deadline across sequential providers in one sync request thread.

    Nested scopes cannot extend an outer deadline. Workers receive a timeout explicitly;
    thread-local request state is deliberately not inherited by detached SDK workers.
    """
    previous = getattr(_request_state, "deadline", None)
    deadline = time.monotonic() + max(0.0, timeout_s)
    _request_state.deadline = min(previous, deadline) if previous is not None else deadline
    try:
        yield
    finally:
        _request_state.deadline = previous


def remaining_budget(max_seconds: float) -> float:
    deadline = getattr(_request_state, "deadline", None)
    if deadline is None:
        return max(0.0, max_seconds)
    return max(0.0, min(max_seconds, deadline - time.monotonic()))


def provider_call(fn: Callable[[], R], timeout_s: float) -> R:
    """Bound wall time even when an SDK ignores its socket timeout.

    Python cannot cancel a running sync SDK call. Timed-out workers are daemon threads;
    their results are discarded, and their slot is held until the call exits. The global
    cap prevents repeated requests from accumulating unbounded workers. Saturation fails
    immediately into the existing deterministic fallback, without queueing more calls.
    """
    timeout_s = remaining_budget(timeout_s)
    if timeout_s <= 0:
        raise ProviderTimeout("request time budget spent")
    slots = _provider_slots
    if not slots.acquire(blocking=False):
        raise LLMUnavailable("provider workers busy")
    done = threading.Event()
    result: list[tuple[bool, Any]] = []

    def run() -> None:
        try:
            result.append((True, fn()))
        except Exception as exc:  # noqa: BLE001 - forward failures to the request thread
            result.append((False, exc))
        finally:
            slots.release()
            done.set()

    try:
        threading.Thread(target=run, daemon=True, name="qadam-provider").start()
    except RuntimeError:
        slots.release()
        raise LLMUnavailable("provider worker unavailable") from None
    if not done.wait(timeout_s):
        raise ProviderTimeout("provider time budget spent")
    success, value = result[0]
    if not success:
        raise value
    return value


class _ModelFailed(Exception):
    """One model could not answer; the next model in the chain may still work."""

    def __init__(self, reason: str, *, network: bool = False) -> None:
        super().__init__(reason)
        self.network = network


class _HardFailure(Exception):
    """Credentials are rejected: no other model will work either."""


class LLMClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.model_name = settings.claude_model
        self._client: Any = None  # anthropic.Anthropic, created lazily; tests inject a fake
        self._genai: Any = None  # google.genai.Client, created lazily; tests inject a fake
        self._lock = threading.Lock()
        self._local = threading.local()
        self._cooldown_until = 0.0

    # ------------------------------------------------------------------ state

    @property
    def available(self) -> bool:
        return self.settings.has_llm and time.monotonic() >= self._cooldown_until

    @property
    def last_model(self) -> str | None:
        """Model that produced the last structured answer in this thread ("<id> (cache)")."""
        return getattr(self._local, "model", None)

    def _cool_down(self, seconds: float, reason: str) -> None:
        self._cooldown_until = time.monotonic() + seconds
        log.warning("LLM cooldown %.0fs: %s", seconds, reason)

    def _models(self) -> list[str]:
        models = [self.settings.claude_model, self.settings.claude_fallback_model]
        return list(dict.fromkeys(m for m in models if m))

    def _get_client(self) -> Any:
        if self._client is None:
            with self._lock:
                if self._client is None:
                    import anthropic

                    self._client = anthropic.Anthropic(
                        api_key=self.settings.anthropic_api_key,
                        timeout=self.settings.llm_timeout_s,
                        max_retries=SDK_RETRIES,
                        default_headers=self.settings.anthropic_headers,
                    )
        return self._client

    # ------------------------------------------------------------- structured

    def generate_structured(
        self, *, system: str, prompt: str, schema: type[T], effort: Effort = "low"
    ) -> T:
        """Return a validated `schema` instance or raise LLMUnavailable."""
        self._local.model = None
        if not self.settings.has_llm:
            raise LLMUnavailable("LLM disabled or ANTHROPIC_API_KEY is empty")
        if time.monotonic() < self._cooldown_until:
            raise LLMUnavailable("LLM is cooling down after a recent failure")

        key = self._cache_key(system, prompt, schema, effort)
        cached = self._cache_read(key, schema)
        if cached is not None:
            obj, model = cached
            self._local.model = f"{model} (cache)"
            return obj

        started = time.monotonic()
        budget = remaining_budget(min(self.settings.llm_timeout_s, REQUEST_BUDGET_S))
        deadline = started + budget
        reasons: list[str] = []
        all_network = True
        models = self._models()
        for i, model in enumerate(models):
            remaining = remaining_budget(deadline - time.monotonic())
            if remaining <= 0:
                reasons.append(f"{model}: skipped, time budget spent")
                break
            # Leave time for the fallback model when the primary stalls.
            attempt_timeout = remaining * 0.7 if i + 1 < len(models) else remaining
            try:
                obj, served = provider_call(
                    lambda model=model, attempt_timeout=attempt_timeout: self._call(
                        model, system, prompt, schema, effort,
                        retries=SDK_RETRIES, timeout_s=attempt_timeout,
                    ),
                    attempt_timeout,
                )
            except ProviderTimeout:
                reasons.append(f"{model}: timeout")
                continue
            except _HardFailure as exc:
                self._cool_down(AUTH_COOLDOWN_S, str(exc))
                raise LLMUnavailable(str(exc)) from exc
            except _ModelFailed as exc:
                all_network = all_network and exc.network
                reasons.append(f"{model}: {exc}")
                log.warning("LLM model %s failed: %s", model, exc)
                continue
            self._local.model = served
            self._cache_write(key, schema, obj, served)
            log.info("LLM %s ok via %s in %.1fs", schema.__name__, served,
                     time.monotonic() - started)
            return obj

        if all_network and reasons:
            self._cool_down(NETWORK_COOLDOWN_S, "all models failed on network/timeouts")
        raise LLMUnavailable("; ".join(reasons) or "no model configured")

    def _call(
        self,
        model: str,
        system: str,
        prompt: str,
        schema: type[T],
        effort: Effort,
        *,
        retries: int,
        timeout_s: float,
    ) -> tuple[T, str]:
        import anthropic

        client = self._get_client().with_options(max_retries=retries, timeout=timeout_s)
        try:
            resp = client.beta.messages.parse(
                model=model,
                max_tokens=MAX_TOKENS,
                system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": prompt}],
                output_format=schema,
                output_config={"effort": effort},
                betas=[FALLBACK_BETA],
                fallbacks="default",
            )
        except ValidationError as exc:
            # The SDK validates inside parse(): refused or truncated text lands here.
            raise _ModelFailed(f"invalid structured output ({exc.error_count()} errors)") from exc
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as exc:
            raise _HardFailure(f"credentials rejected (HTTP {exc.status_code})") from exc
        except anthropic.APITimeoutError as exc:
            raise _ModelFailed("timeout", network=True) from exc
        except anthropic.APIConnectionError as exc:
            raise _ModelFailed("connection error", network=True) from exc
        except anthropic.RateLimitError as exc:
            raise _ModelFailed("rate limited (429)", network=True) from exc
        except anthropic.APIStatusError as exc:
            # 400 (bad request / credit balance), 404 (unknown model), 5xx / 529 after retries
            raise _ModelFailed(f"HTTP {exc.status_code}", network=exc.status_code >= 500) from exc

        if resp.stop_reason in ("refusal", "max_tokens"):
            raise _ModelFailed(f"stop_reason={resp.stop_reason}")
        parsed = resp.parsed_output
        if parsed is None:
            raise _ModelFailed(f"no parsed output (stop_reason={resp.stop_reason})")
        if not isinstance(parsed, schema):
            parsed = schema.model_validate(parsed)
        return parsed, str(getattr(resp, "model", None) or model)

    # ------------------------------------------------------------------ cache

    @property
    def _llm_cache_dir(self) -> Path:
        return self.settings.cache_dir / "llm"

    def _cache_key(self, system: str, prompt: str, schema: type[BaseModel], effort: str) -> str:
        schema_json = json.dumps(schema.model_json_schema(), sort_keys=True)
        payload = {
            "v": CACHE_VERSION,
            "models": self._models(),
            "system": system,
            "prompt": prompt,
            "schema": schema.__name__,
            "schema_sha": hashlib.sha256(schema_json.encode("utf-8")).hexdigest(),
            "effort": effort,
        }
        raw = json.dumps(payload, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _cache_read(self, key: str, schema: type[T]) -> tuple[T, str] | None:
        path = self._llm_cache_dir / f"{key}.json"
        if not path.exists():
            return None
        try:
            entry = json.loads(path.read_text(encoding="utf-8"))
            return schema.model_validate(entry["output"]), str(entry.get("model") or "?")
        except Exception as exc:  # noqa: BLE001 - a broken cache entry must never break a request
            log.warning("LLM cache entry %s unreadable (%s), ignoring",
                        key[:12], type(exc).__name__)
            path.unlink(missing_ok=True)
            return None

    def _cache_write(self, key: str, schema: type[BaseModel], obj: BaseModel, model: str) -> None:
        # Only the model output is stored — never the prompt (it may carry the user's query) —
        # and output fields that echo the user's text are blanked (QueryIntent.raw_query).
        output = obj.model_dump(mode="json")
        for name in REDACTED_FIELDS:
            if isinstance(output.get(name), str):
                output[name] = ""
        entry = {
            "v": CACHE_VERSION,
            "schema": schema.__name__,
            "model": model,
            "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "output": output,
        }
        _atomic_write_text(self._llm_cache_dir / f"{key}.json",
                           json.dumps(entry, ensure_ascii=False))

    # ------------------------------------------------------------- embeddings

    def _get_genai(self) -> Any:
        if self._genai is None:
            with self._lock:
                if self._genai is None:
                    from google import genai
                    from google.genai import types

                    self._genai = genai.Client(
                        api_key=self.settings.gemini_api_key,
                        http_options=types.HttpOptions(timeout=EMBED_TIMEOUT_MS),
                    )
        return self._genai

    def embed_query(self, text: str) -> np.ndarray | None:
        """L2-normalised query vector, or None without GEMINI_API_KEY / on any failure
        (retrieval goes lexical)."""
        if not self.settings.has_embeddings or not text.strip():
            return None
        try:
            timeout_s = remaining_budget(QUERY_EMBED_TIMEOUT_S)
            return provider_call(
                lambda: self._embed([text], "RETRIEVAL_QUERY", online_timeout_s=timeout_s)[0],
                timeout_s,
            )
        except Exception as exc:  # noqa: BLE001 - embeddings are optional, lexical works
            log.warning("embed_query failed (%s), retrieval goes lexical", type(exc).__name__)
            return None

    def embed_documents(self, texts: list[str]) -> np.ndarray:
        """(len(texts), embed_dim) float32, L2-normalised. Batches internally.
        Raise LLMUnavailable without GEMINI_API_KEY (build_index then skips the index)."""
        if not self.settings.has_embeddings:
            raise LLMUnavailable("GEMINI_API_KEY is not set: no embeddings, retrieval is lexical")
        if not texts:
            return np.zeros((0, self.settings.embed_dim), dtype=np.float32)
        try:
            return self._embed(texts, "RETRIEVAL_DOCUMENT")
        except LLMUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001
            raise LLMUnavailable(f"embedding failed: {type(exc).__name__}") from exc

    def _embed(
        self, texts: list[str], task_type: str, *, online_timeout_s: float | None = None
    ) -> np.ndarray:
        dim = self.settings.embed_dim
        out = np.zeros((len(texts), dim), dtype=np.float32)
        keys = [self._embed_key(t, task_type) for t in texts]
        missing: list[int] = []
        for i, key in enumerate(keys):
            vec = self._embed_cache_read(key)
            if vec is None:
                missing.append(i)
            else:
                out[i] = vec
        for start in range(0, len(missing), EMBED_BATCH):
            idx = missing[start : start + EMBED_BATCH]
            vectors = self._embed_batch(
                [texts[i] for i in idx], task_type, online_timeout_s=online_timeout_s
            )
            for i, vec in zip(idx, vectors, strict=True):
                out[i] = vec
                self._embed_cache_write(keys[i], vec)
        return out

    def _embed_batch(
        self, texts: list[str], task_type: str, *, online_timeout_s: float | None = None
    ) -> np.ndarray:
        from google.genai import types

        config = types.EmbedContentConfig(
            task_type=task_type, output_dimensionality=self.settings.embed_dim,
            http_options=types.HttpOptions(
                timeout=max(1, int(online_timeout_s * 1000)),
                retry_options=types.HttpRetryOptions(attempts=1),
            ) if online_timeout_s is not None else None,
        )
        pause = EMBED_PAUSE_S
        attempts = 1 if online_timeout_s is not None else EMBED_ATTEMPTS
        for attempt in range(1, attempts + 1):
            try:
                resp = self._get_genai().models.embed_content(
                    model=self.settings.embed_model, contents=texts, config=config
                )
                return _normalise_rows(
                    [e.values for e in resp.embeddings], len(texts), self.settings.embed_dim
                )
            except LLMUnavailable:
                raise
            except Exception as exc:  # noqa: BLE001 - retried, then surfaced as LLMUnavailable
                if attempt == attempts:
                    raise LLMUnavailable(f"Gemini embed failed: {type(exc).__name__}") from exc
                log.warning("Gemini embed attempt %d failed (%s), retrying", attempt,
                            type(exc).__name__)
                time.sleep(pause)
                pause *= 2
        raise LLMUnavailable("Gemini embed failed")  # pragma: no cover - loop always returns

    def _embed_key(self, text: str, task_type: str) -> str:
        raw = f"{self.settings.embed_model}|{self.settings.embed_dim}|{task_type}|{text}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _embed_cache_read(self, key: str) -> np.ndarray | None:
        path = self.settings.cache_dir / "embed" / f"{key}.npy"
        if not path.exists():
            return None
        try:
            vec = np.load(path).astype(np.float32)
        except Exception:  # noqa: BLE001
            path.unlink(missing_ok=True)
            return None
        return vec if vec.shape == (self.settings.embed_dim,) else None

    def _embed_cache_write(self, key: str, vec: np.ndarray) -> None:
        path = self.settings.cache_dir / "embed" / f"{key}.npy"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp")
            with tmp.open("wb") as fh:
                np.save(fh, vec.astype(np.float32))
            os.replace(tmp, path)
        except OSError as exc:
            log.warning("embedding cache write failed: %s", type(exc).__name__)


def _normalise_rows(rows: list[Any], n: int, dim: int) -> np.ndarray:
    arr = np.asarray(rows, dtype=np.float32)
    if arr.shape != (n, dim):
        raise LLMUnavailable(f"embedding shape {arr.shape} != ({n}, {dim})")
    norms = np.linalg.norm(arr, axis=1, keepdims=True)
    if np.any(norms == 0):
        raise LLMUnavailable("zero embedding vector")
    return arr / norms


def _atomic_write_text(path: Path, text: str) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        log.warning("LLM cache write failed: %s", type(exc).__name__)
