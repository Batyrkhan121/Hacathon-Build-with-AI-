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
import unicodedata
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal, TypeVar

import numpy as np
from pydantic import BaseModel, ValidationError

from app.config import Settings

log = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)
Effort = Literal["low", "medium", "high"]

FALLBACK_BETA = "server-side-fallback-2026-07-01"
MAX_TOKENS = 8192  # adaptive thinking shares this budget; cards need ~500 output tokens
SDK_RETRIES = 2  # primary model; the fallback model gets one retry
FALLBACK_RETRIES = 1
TOTAL_BUDGET_FACTOR = 3.0  # do not start the fallback model after 3 x llm_timeout_s
NETWORK_COOLDOWN_S = 60.0
AUTH_COOLDOWN_S = 300.0
CACHE_VERSION = 1
REDACTED_FIELDS = ("raw_query",)  # never written to the disk cache (callers re-fill them)

EMBED_BATCH = 100
EMBED_ATTEMPTS = 3
EMBED_PAUSE_S = 1.0  # doubled after every failed attempt
EMBED_TIMEOUT_MS = 15_000  # index build (embed_documents)
# A live query must not wait for Gemini: one short attempt, then lexical search for a while
# (the shared Gemini quota can be exhausted for hours).
EMBED_QUERY_ATTEMPTS = 1
EMBED_QUERY_TIMEOUT_MS = 5_000
EMBED_COOLDOWN_S = NETWORK_COOLDOWN_S
EMBED_DAILY_COOLDOWN_S = 30 * 60  # free tier: the daily quota resets once a day
CONFIG_COOLDOWN_S = 300.0  # no API credits / unknown model: nothing helps for a while
_NETWORK_KINDS = frozenset({"timeout", "network", "quota", "server"})
_CONFIG_KINDS = frozenset({"no_credit", "bad_model"})
# Most telling reason first when every model failed.
_KIND_PRIORITY = ("no_credit", "auth", "bad_model", "quota", "timeout", "network", "server",
                  "refusal", "invalid_output", "bad_request")

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"
_INVISIBLE_RE = re.compile("[​-‏‪-‮⁠-⁤﻿]")
_ANGLE_RE = re.compile("[<>〈〉〈〉⟨⟩＜＞﹤﹥]")


@lru_cache(maxsize=16)
def load_prompt(name: str) -> str:
    """Text of prompts/<name>.md (utf-8)."""
    return (PROMPTS_DIR / f"{name}.md").read_text(encoding="utf-8")


def user_data_block(text: str) -> str:
    """The user's text as a delimited DATA block for a prompt.

    No tag can be formed inside: NFKC folds look-alikes (fullwidth ＜ ＞ -> < >), invisible
    characters (zero-width, bidi controls) are dropped, then every angle bracket becomes a
    space. So «<</user_query>/user_query>» cannot leave even a false closing tag behind.
    """
    clean = _INVISIBLE_RE.sub("", unicodedata.normalize("NFKC", text))
    clean = re.sub(r"[ \t]{2,}", " ", _ANGLE_RE.sub(" ", clean)).strip()
    return f"<user_query>\n{clean}\n</user_query>"


class LLMUnavailable(RuntimeError):
    """Raised when the LLM cannot answer (no key, quota, timeout, refusal, invalid output)."""


class _ModelFailed(Exception):
    """One model could not answer; the next model in the chain may still work."""

    def __init__(self, reason: str, *, kind: str) -> None:
        super().__init__(reason)
        self.kind = kind


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
        self._cooldown_kind: str | None = None
        self._embed_cooldown_until = 0.0  # Gemini, separate from Claude
        self._embed_cooldown_kind = "error"

    # ------------------------------------------------------------------ state

    @property
    def available(self) -> bool:
        return self.settings.has_llm and time.monotonic() >= self._cooldown_until

    @property
    def cooldown_kind(self) -> str | None:
        """Why the client is cooling down right now (None when it is not)."""
        return self._cooldown_kind if time.monotonic() < self._cooldown_until else None

    @property
    def enabled(self) -> bool:
        """LLM_ENABLED: an explicit switch-off (then not even the disk cache is used)."""
        return self.settings.llm_enabled

    @property
    def last_model(self) -> str | None:
        """Model that produced the last structured answer in this thread ("<id> (cache)")."""
        return getattr(self._local, "model", None)

    @property
    def last_error(self) -> str | None:
        """Why the last structured call in this thread failed (None after a success):
        disabled / no_key / no_credit / auth / bad_model / quota / timeout / network / server /
        refusal / invalid_output / bad_request."""
        return getattr(self._local, "error", None)

    @property
    def last_embed_status(self) -> str | None:
        """Last embed_query in this thread: ok / cache / no_key / empty / quota / daily_quota /
        network / error (during a cooldown: the kind of the failure that started it)."""
        return getattr(self._local, "embed", None)

    def _cool_down(self, seconds: float, reason: str, kind: str) -> None:
        self._cooldown_until = time.monotonic() + seconds
        self._cooldown_kind = kind
        log.warning("LLM cooldown %.0fs (%s): %s", seconds, kind, reason)

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
        """Return a validated `schema` instance or raise LLMUnavailable (reason: last_error).

        Order: switched off -> nothing; then the disk cache (it serves preset scenarios even
        without a key or during a cooldown); then no key / cooldown -> fail fast."""
        self._local.model = None
        self._local.error = None
        if not self.settings.llm_enabled:
            self._local.error = "disabled"
            raise LLMUnavailable("LLM disabled (LLM_ENABLED=false)")

        key = self._cache_key(system, prompt, schema, effort)
        cached = self._cache_read(key, schema)
        if cached is not None:
            obj, model = cached
            self._local.model = f"{model} (cache)"
            return obj

        if not self.settings.anthropic_api_key:
            self._local.error = "no_key"
            raise LLMUnavailable("ANTHROPIC_API_KEY is empty")
        if time.monotonic() < self._cooldown_until:
            self._local.error = self._cooldown_kind or "network"
            raise LLMUnavailable("LLM is cooling down after a recent failure")

        started = time.monotonic()
        budget = TOTAL_BUDGET_FACTOR * self.settings.llm_timeout_s
        reasons: list[str] = []
        kinds: list[str] = []
        for i, model in enumerate(self._models()):
            if i > 0 and time.monotonic() - started > budget:
                reasons.append(f"{model}: skipped, time budget spent")
                break
            try:
                obj, served = self._call(
                    model, system, prompt, schema, effort,
                    retries=SDK_RETRIES if i == 0 else FALLBACK_RETRIES,
                )
            except _HardFailure as exc:
                self._cool_down(AUTH_COOLDOWN_S, str(exc), "auth")
                self._local.error = "auth"
                raise LLMUnavailable(str(exc)) from exc
            except _ModelFailed as exc:
                kinds.append(exc.kind)
                reasons.append(f"{model}: {exc}")
                log.warning("LLM model %s failed: %s", model, exc)
                continue
            self._local.model = served
            self._cache_write(key, schema, obj, served)
            log.info("LLM %s ok via %s in %.1fs", schema.__name__, served,
                     time.monotonic() - started)
            return obj

        kind = next((k for k in _KIND_PRIORITY if k in kinds), "bad_request")
        self._local.error = kind
        if kinds and set(kinds) & _CONFIG_KINDS and set(kinds) <= _CONFIG_KINDS | _NETWORK_KINDS:
            self._cool_down(CONFIG_COOLDOWN_S, "; ".join(reasons), kind)
        elif kinds and set(kinds) <= _NETWORK_KINDS:
            self._cool_down(NETWORK_COOLDOWN_S, "all models failed on network/timeouts", kind)
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
    ) -> tuple[T, str]:
        import anthropic

        client = self._get_client().with_options(max_retries=retries)
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
            raise _ModelFailed(f"invalid structured output ({exc.error_count()} errors)",
                               kind="invalid_output") from exc
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as exc:
            raise _HardFailure(f"credentials rejected (HTTP {exc.status_code})") from exc
        except anthropic.APITimeoutError as exc:
            raise _ModelFailed("timeout", kind="timeout") from exc
        except anthropic.APIConnectionError as exc:
            raise _ModelFailed("connection error", kind="network") from exc
        except anthropic.RateLimitError as exc:
            raise _ModelFailed("rate limited (429)", kind="quota") from exc
        except anthropic.APIStatusError as exc:
            # 400 (bad request / credit balance), 404 (unknown model), 5xx / 529 after retries
            raise _ModelFailed(f"HTTP {exc.status_code}", kind=_status_kind(exc)) from exc

        if resp.stop_reason == "refusal":
            raise _ModelFailed("stop_reason=refusal", kind="refusal")
        if resp.stop_reason == "max_tokens":
            raise _ModelFailed("stop_reason=max_tokens", kind="invalid_output")
        parsed = resp.parsed_output
        if parsed is None:
            raise _ModelFailed(f"no parsed output (stop_reason={resp.stop_reason})",
                               kind="invalid_output")
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
        (retrieval goes lexical). One short attempt; after a failure only the disk cache is
        used for EMBED_COOLDOWN_S, so a dead quota never slows a live request down."""
        if not self.settings.has_embeddings:
            self._local.embed = "no_key"
            return None
        if not text.strip():
            self._local.embed = "empty"
            return None
        cooling = time.monotonic() < self._embed_cooldown_until
        key = self._embed_key(text, "RETRIEVAL_QUERY")
        cached = self._embed_cache_read(key)
        if cached is not None:
            self._local.embed = "cache"
            return cached
        if cooling:
            self._local.embed = self._embed_cooldown_kind
            return None
        try:
            vec = self._embed([text], "RETRIEVAL_QUERY", attempts=EMBED_QUERY_ATTEMPTS,
                              timeout_ms=EMBED_QUERY_TIMEOUT_MS)[0]
        except Exception as exc:  # noqa: BLE001 - embeddings are optional, lexical works
            kind = _embed_error_kind(exc)
            pause = EMBED_DAILY_COOLDOWN_S if kind == "daily_quota" else EMBED_COOLDOWN_S
            self._embed_cooldown_until = time.monotonic() + pause
            self._embed_cooldown_kind = kind
            self._local.embed = kind
            log.warning("embed_query failed (%s, %s): lexical search for the next %.0f s",
                        kind, type(exc).__name__, pause)
            return None
        self._local.embed = "ok"
        return vec

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

    def _embed(self, texts: list[str], task_type: str, *, attempts: int = EMBED_ATTEMPTS,
               timeout_ms: int | None = None) -> np.ndarray:
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
            vectors = self._embed_batch([texts[i] for i in idx], task_type,
                                        attempts=attempts, timeout_ms=timeout_ms)
            for i, vec in zip(idx, vectors, strict=True):
                out[i] = vec
                self._embed_cache_write(keys[i], vec)
        return out

    def _embed_batch(self, texts: list[str], task_type: str, *, attempts: int = EMBED_ATTEMPTS,
                     timeout_ms: int | None = None) -> np.ndarray:
        from google.genai import types

        config = types.EmbedContentConfig(
            task_type=task_type, output_dimensionality=self.settings.embed_dim,
            http_options=types.HttpOptions(timeout=timeout_ms) if timeout_ms else None,
        )
        pause = EMBED_PAUSE_S
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


def _status_kind(exc: Any) -> str:
    """HTTP status error of the Anthropic API -> short reason."""
    code = getattr(exc, "status_code", 0) or 0
    text = str(getattr(exc, "message", "") or exc).lower()
    if code == 400 and ("credit" in text or "billing" in text):
        return "no_credit"
    if code == 404:
        return "bad_model"
    if code >= 500:
        return "server"
    return "bad_request"


def _embed_error_kind(exc: BaseException) -> str:
    """Gemini failure -> daily_quota / quota / network / error (the SDK error is the cause of
    our LLMUnavailable; same daily-quota test as pipeline/build_index.is_daily_quota)."""
    chain: list[BaseException] = []
    cur: BaseException | None = exc
    while cur is not None and len(chain) < 5:
        chain.append(cur)
        cur = cur.__cause__ or cur.__context__
    text = " ".join(str(e) for e in chain)
    if "RESOURCE_EXHAUSTED" in text and "PerDay" in text:
        return "daily_quota"
    if "RESOURCE_EXHAUSTED" in text or "429" in text:
        return "quota"
    names = " ".join(type(e).__name__ for e in chain)
    if any(isinstance(e, (TimeoutError, ConnectionError, OSError)) for e in chain) or any(
            n in names for n in ("Timeout", "ConnectError", "ConnectionError", "NetworkError")):
        return "network"
    return "error"


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
