"""HTTP API + static web UI. Owner: LLM lane (task T34).

Endpoints (contract):
  GET  /api/health     -> {"status": "ok", "poi_count": int, "index": "ready|lexical",
                           "llm": "ok|off"}; HTTP 503 when poi_count == 0 (same body,
                           status "degraded"), any other degraded state is 200
  GET  /api/landmarks  -> list[Landmark]
  GET  /api/meta       -> {"snapshot_date", "poi_count", "by_category": {...}, "sources": [...]}
  POST /api/recommend  RecommendRequest -> Answer   (422 on invalid input / unknown landmark)
  /                    -> web/ (StaticFiles, html=True)
Privacy: log only request_id, latency, h3 resolution and counts — never the cell + query together.

T34: turn this into a factory `create_app(deps: Deps | None = None) -> FastAPI` (tests inject a
synthetic Store + fake LLM) and keep a module-level `app = create_app()` for
`uvicorn app.api:app`. Mount web/ last so /api routes win.

Notes:
- Without injected deps the Store is loaded in the lifespan; if that fails the process still
  starts: /api/health says "unavailable" (503) and data endpoints answer 503, the UI still loads.
- /api/recommend is a sync `def`: the pipeline is blocking (SDK calls, numpy), FastAPI runs it
  in the threadpool. The cheap read-only endpoints are `async def`, so a burst of slow
  recommendations cannot starve /api/health. At most MAX_CONCURRENT_LLM requests use the LLM
  at once; the rest are answered on the deterministic path (fast, no quota) with a warning.
- Request bodies on /api/* are capped at MAX_BODY_BYTES (413); a real request is < 2 KB.
- An unexpected error in /api/recommend is logged by type and stack frames only: exception
  messages may contain the request body (query + cell), which must never reach the logs.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import traceback
from collections import Counter
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.concurrency import run_in_threadpool
from fastapi.exceptions import ResponseValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from app import generate
from app.config import ROOT, get_settings
from app.llm import LLMClient, load_prompt
from app.pipeline import Deps, InvalidRequest, index_meta, index_problem, index_status, recommend
from app.schemas import SCHEMA_VERSION, Answer, Landmark, RecommendRequest
from app.store import load_store

log = logging.getLogger("qadam.api")

WEB_DIR = ROOT / "web"
# The WEB lane serves web/ with `python -m http.server 5500` while developing.
DEV_ORIGINS = ["http://localhost:5500", "http://127.0.0.1:5500"]
MAX_BODY_BYTES = 16 * 1024
MAX_CONCURRENT_LLM = 8
BUSY_TEXT = {
    "ru": "Сервер занят — этот ответ собран без ИИ.",
    "kk": "Сервер бос емес — бұл жауап ЖИ-сыз жиналды.",
    "en": "The server is busy — this answer was built without AI.",
}
INTERNAL_ERROR = "Внутренняя ошибка сервера."
RECOMMEND_RESPONSES: dict[int | str, dict[str, Any]] = {
    413: {"description": f"Тело запроса больше {MAX_BODY_BYTES // 1024} КБ."},
    422: {"description": "Неверный ввод: схема запроса (detail — список ошибок), неизвестный "
                         "ориентир или некорректная H3-ячейка, `at` вне 2000–2100 "
                         "(detail — строка)."},
    500: {"description": "Внутренняя ошибка (без подробностей)."},
    503: {"description": "Данные ещё не загружены."},
}

ASGIApp = Callable[[dict, Callable[[], Awaitable[dict]], Callable[[dict], Awaitable[None]]],
                   Awaitable[None]]


def configure_logging(level: str) -> None:
    """INFO lines of the app.* / qadam.* loggers under uvicorn. The root logger stays quiet on
    purpose: httpx would log Open-Meteo URLs with coordinates, SDKs log bodies at DEBUG."""
    for name in ("app", "qadam"):
        logger = logging.getLogger(name)
        try:
            logger.setLevel(level.upper())
        except ValueError:
            logger.setLevel(logging.INFO)
        if not logger.handlers:
            handler = logging.StreamHandler()
            handler.setFormatter(
                logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
            logger.addHandler(handler)
    for noisy in ("httpx", "httpcore", "httpx2", "anthropic", "google_genai"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


class ApiGuard:
    """Pure ASGI middleware for /api/*: body-size cap (413) and one access-log line per call
    (method, path, status, latency — never the body)."""

    def __init__(self, app: ASGIApp, max_body: int = MAX_BODY_BYTES) -> None:
        self.app = app
        self.max_body = max_body

    async def __call__(self, scope: dict, receive: Callable, send: Callable) -> None:
        if scope["type"] != "http" or not scope["path"].startswith("/api/"):
            await self.app(scope, receive, send)
            return
        started = time.perf_counter()
        status_code = 500

        async def send_wrapper(message: dict) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
            await send(message)

        try:
            if scope["method"] in ("POST", "PUT", "PATCH"):
                body = await self._read_body(scope, receive)
                if body is None:
                    await _send_json(send_wrapper, 413, {"detail": RECOMMEND_RESPONSES[413][
                        "description"]})
                    return
                receive = _replay(body, receive)
            await self.app(scope, receive, send_wrapper)
        finally:
            log.info("%s %s -> %d in %.0f ms", scope["method"], scope["path"], status_code,
                     (time.perf_counter() - started) * 1000)

    async def _read_body(self, scope: dict, receive: Callable) -> bytes | None:
        """Whole body, or None as soon as it is known to exceed the cap."""
        for name, value in scope.get("headers", []):
            if name == b"content-length" and value.isdigit() and int(value) > self.max_body:
                return None
        chunks: list[bytes] = []
        size = 0
        while True:
            message = await receive()
            if message["type"] != "http.request":
                break
            chunk = message.get("body", b"")
            size += len(chunk)
            if size > self.max_body:
                return None
            chunks.append(chunk)
            if not message.get("more_body", False):
                break
        return b"".join(chunks)


def _replay(body: bytes, receive: Callable) -> Callable[[], Awaitable[dict]]:
    sent = False

    async def replay() -> dict:
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        return await receive()  # later calls wait for the real disconnect

    return replay


async def _send_json(send: Callable, status_code: int, content: dict[str, Any]) -> None:
    payload = json.dumps(content, ensure_ascii=False).encode("utf-8")
    await send({"type": "http.response.start", "status": status_code,
                "headers": [(b"content-type", b"application/json"),
                            (b"content-length", str(len(payload)).encode())]})
    await send({"type": "http.response.body", "body": payload})


class NoCacheStaticFiles(StaticFiles):
    """web/ with `Cache-Control: no-cache` (WEB request): the browser revalidates every file
    (ETag / Last-Modified -> cheap 304), so an old index.html never meets a new app.js."""

    def file_response(self, *args: Any, **kwargs: Any) -> Response:
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


NO_POI_DETAIL = "Данные POI не загружены: data/processed/poi.jsonl пуст или не прочитан."


def _store_problems(d: Deps) -> list[str]:
    """What makes the service degraded (the UI and the jury must not see «nothing found»).
    Store.warnings carry the human details (QUALITY's store.py); the stale flag itself is
    read from meta.json, not parsed out of a warning text."""
    out: list[str] = []
    if not d.store.pois:
        out.append("нет POI: data/processed/poi.jsonl пуст или не прочитан")
    if not d.store.landmarks:
        out.append("нет ориентиров: data/landmarks.json пуст или не прочитан")
    # «устарел» only about vectors in use: an index of another model / a broken one is not
    # used at all, and the line below about it is the news (Store does the same)
    if (index_status(d.settings) == "stale" and d.store.embeddings is not None
            and not any("устарел" in w for w in d.store.warnings)):
        out.append("индекс эмбеддингов устарел (meta.json: status=stale)")
    problem = index_problem(d.settings, d.store)
    if problem == "index_mismatch":
        meta = index_meta(d.settings) or {}
        text = (f"индекс построен для {meta.get('model')}/{meta.get('dim')}, а в настройках "
                f"{d.settings.embed_model}/{d.settings.embed_dim}: поиск только по словам")
        if not any(d.settings.embed_model in w for w in d.store.warnings):
            out.append(text)
        else:
            out.append("индекс не подходит к модели запросов: поиск только по словам")
    elif problem == "index_missing":
        out.append("индекс эмбеддингов не загружен: поиск только по словам")
    return out


def _status(d: Deps, problems: list[str]) -> str:
    """degraded: no POI / landmarks, a missing prompt, or a stale index (meta.json flag)."""
    return "degraded" if problems or index_status(d.settings) == "stale" else "ok"


def check_prompts() -> list[str]:
    """Startup check: a missing prompt must fail loudly, not look like «LLM unavailable» on
    every request. The app still starts (the deterministic path needs no prompt)."""
    problems: list[str] = []
    checks = (("generate_cards", generate.check_prompt, "тексты карточек ИИ недоступны"),
              ("parse_query", lambda: load_prompt("parse_query"), "разбор запроса ИИ недоступен"))
    for name, check, effect in checks:
        try:
            check()
        except Exception as exc:  # noqa: BLE001 - reported in /api/health, logged as error
            log.error("prompt check failed: prompts/%s.md (%s)", name, type(exc).__name__)
            problems.append(f"нет промпта prompts/{name}.md: {effect}")
    return problems


def _index_state(d: Deps) -> str:
    if d.store.embeddings is None:
        problem = index_problem(d.settings, d.store)
        return {"index_mismatch": "lexical (index mismatch)",
                "index_missing": "lexical (index missing)"}.get(problem or "", "lexical")
    return "ready" if d.settings.has_embeddings else "ready (no key)"


def _llm_state(d: Deps) -> str:
    """"ok" or "off (<reason>)": disabled / no_key / no client / the cooldown reason."""
    if d.llm is None:
        return "off (no client)"
    if not d.settings.llm_enabled:
        return "off (disabled)"
    if not d.settings.anthropic_api_key:
        return "off (no_key)"
    if not d.llm.available:
        return f"off ({getattr(d.llm, 'cooldown_kind', None) or 'cooldown'})"
    return "ok"


def build_default_deps() -> Deps:
    settings = get_settings()
    return Deps(store=load_store(settings), llm=LLMClient(settings), settings=settings)


async def get_deps(request: Request) -> Deps:
    deps: Deps | None = request.app.state.deps
    if deps is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Данные не загружены: проверьте data/processed/poi.jsonl и логи сервера.",
        )
    return deps


DepsDep = Annotated[Deps, Depends(get_deps)]


def create_app(deps: Deps | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if app.state.deps is None:
            try:
                app.state.deps = await run_in_threadpool(build_default_deps)
                log.info("store loaded: %d POI", len(app.state.deps.store.pois))
            except Exception:  # noqa: BLE001 - serve the UI and a clear 503 instead
                # No request data exists at startup, so the full traceback is safe to log.
                log.exception("store failed to load; data endpoints answer 503")
        yield

    app = FastAPI(title="Qadam API", version=SCHEMA_VERSION, lifespan=lifespan)
    app.state.deps = deps
    app.state.prompt_problems = check_prompts()
    llm_slots = threading.BoundedSemaphore(MAX_CONCURRENT_LLM)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=DEV_ORIGINS,
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type"],
    )
    app.add_middleware(ApiGuard)

    @app.exception_handler(ResponseValidationError)
    async def response_invalid(request: Request, exc: ResponseValidationError) -> JSONResponse:
        # The message would echo the Answer (incl. the user's query): log the type only.
        log.error("response validation failed on %s (%d errors)", request.url.path,
                  len(exc.errors()))
        return JSONResponse(status_code=500, content={"detail": INTERNAL_ERROR})

    @app.get("/api/health", tags=["api"], summary="Состояние сервиса",
             responses={503: {"description": "Нет POI (poi_count 0) или данные не загрузились"}})
    async def health(request: Request) -> JSONResponse:
        d: Deps | None = request.app.state.deps
        if d is None:
            return JSONResponse(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                content={"status": "unavailable", "poi_count": 0, "index": "lexical",
                         "llm": "off", "schema_version": SCHEMA_VERSION},
            )
        problems = [*_store_problems(d), *request.app.state.prompt_problems]
        # no POI = every recommendation is 503: a hosting platform's HTTP check sees only the
        # code, so an empty server must fail it. Other degraded states still answer cards: 200
        code = status.HTTP_503_SERVICE_UNAVAILABLE if not d.store.pois else status.HTTP_200_OK
        return JSONResponse(status_code=code, content={
            "status": _status(d, problems),
            "poi_count": len(d.store.pois),
            "index": _index_state(d),
            "llm": _llm_state(d),
            "warnings": [*problems, *d.store.warnings],
            "schema_version": SCHEMA_VERSION,
        })

    @app.get("/api/landmarks", response_model=list[Landmark], tags=["api"],
             summary="Ориентиры для выбора точки старта")
    async def landmarks(d: DepsDep) -> list[Landmark]:
        return sorted(d.store.landmarks.values(), key=lambda lm: lm.id)

    @app.get("/api/meta", tags=["api"], summary="Сведения о снимке данных")
    async def meta(request: Request, d: DepsDep) -> dict[str, Any]:
        pois = d.store.pois
        problems = [*_store_problems(d), *request.app.state.prompt_problems]
        return {
            "status": _status(d, problems),
            "snapshot_date": d.store.snapshot_date,
            "poi_count": len(pois),
            "by_category": dict(sorted(Counter(p.category for p in pois).items())),
            "sources": sorted({s for p in pois for s in p.sources}),
            "index": _index_state(d),
            "warnings": [*problems, *d.store.warnings],
            "schema_version": SCHEMA_VERSION,
        }

    @app.post("/api/recommend", response_model=Answer, tags=["api"],
              summary="1–3 рекомендации рядом", responses=RECOMMEND_RESPONSES)
    def recommend_endpoint(req: RecommendRequest, d: DepsDep) -> Answer:
        if not d.store.pois:  # an empty store would answer «nothing found»: that is a lie
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                                detail=NO_POI_DETAIL)
        acquired = req.use_llm and llm_slots.acquire(blocking=False)
        busy = req.use_llm and not acquired
        if busy:
            req = req.model_copy(update={"use_llm": False})
        try:
            answer = recommend(req, d)
        except InvalidRequest as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except Exception as exc:  # noqa: BLE001 - see the module note on logging
            # Caught here, not by a global handler: Starlette re-raises from its error
            # middleware and the server would log the message (it may echo the request).
            frames = "".join(traceback.format_tb(exc.__traceback__)[-4:])
            log.error("recommend failed: %s\n%s", type(exc).__name__, frames)
            raise HTTPException(status_code=500, detail=INTERNAL_ERROR) from None
        finally:
            if acquired:
                llm_slots.release()
        if busy:
            answer.warnings.insert(0, BUSY_TEXT[answer.lang])
        return answer

    if WEB_DIR.is_dir():  # last, so /api/* routes win
        app.mount("/", NoCacheStaticFiles(directory=WEB_DIR, html=True), name="web")
    return app


configure_logging(get_settings().log_level)
app = create_app()
