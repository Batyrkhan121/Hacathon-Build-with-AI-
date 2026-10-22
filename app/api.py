"""HTTP API + static web UI. Owner: LLM lane (task T34).

Endpoints (contract):
  GET  /api/health     -> {"status": "ok", "poi_count": int, "index": "ready|lexical",
                           "llm": "ok|off"}; HTTP 503 when poi_count == 0 (same body,
                           status "degraded"), any other degraded state is 200
  GET  /api/landmarks  -> list[Landmark]
  GET  /api/meta       -> {"snapshot_date", "poi_count", "by_category": {...}, "sources": [...]}
  POST /api/recommend  RecommendRequest -> Answer   (422 on invalid input / unknown landmark)
  GET  /api/city-gaps  -> CityGaps: data/processed/city_gaps.json as is (app/city_gaps.py, read
                          and strictly checked once at start; Cache-Control public, max-age=3600;
                          no parameters, extra ones ignored and never logged); 503
                          {"detail": "city gaps unavailable"} without a good file;
                          /api/health says "city_gaps": "ready" | "missing"
  /                    -> web/ (StaticFiles, html=True)
Privacy: log only request_id, latency, h3 resolution and counts — never the cell + query together.
Public demo (T161, ARCHITECTURE §5 «Публичное демо»): POST /api/recommend is limited per client IP
(QADAM_RATE_PER_MIN, QADAM_RATE_PER_DAY by the Astana day; 0 = off) in ApiGuard: 429
{"detail": "too many requests"} with Retry-After. The IP is client.host, or behind
QADAM_TRUSTED_PROXY_HOPS proxies the X-Forwarded-For entry the last trusted one added (with 0
the header is never read); it is never logged, the counters are keyed by a salted hash (a
random salt per process), at most RATE_MAX_CLIENTS of them. /api/health also says llm_budget
(none | ok | reached | misconfigured, no sums: app/llm.py).

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
- Request validation errors return fixed metadata: raw input must not reach responses or logs.
- An unexpected error in /api/recommend is logged by type and stack frames only: exception
  messages may contain the request body (query + cell), which must never reach the logs.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import secrets
import threading
import time
import traceback
from collections import Counter, OrderedDict
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from datetime import time as dt_time
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.concurrency import run_in_threadpool
from fastapi.exceptions import RequestValidationError, ResponseValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from app import city_gaps, generate, weather
from app.config import ROOT, get_settings
from app.hours import ASTANA_TZ
from app.llm import LLMClient, load_prompt
from app.pipeline import Deps, InvalidRequest, index_meta, index_problem, index_status, recommend
from app.schemas import SCHEMA_VERSION, Answer, CityGaps, Landmark, RecommendRequest
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
    """Pure ASGI middleware for /api/*: body-size cap (413), the per-IP rate limit of
    POST /api/recommend (429, T161) and one access-log line per call (method, path, status,
    latency — never the body, the query string or the client address)."""

    def __init__(self, app: ASGIApp, max_body: int = MAX_BODY_BYTES,
                 limiter: RateLimiter | None = None) -> None:
        self.app = app
        self.max_body = max_body
        self.limiter = limiter if limiter is not None and limiter.enabled else None

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
            if (self.limiter is not None and scope["method"] == "POST"
                    and scope["path"] == RATE_LIMITED_PATH):
                retry = self.limiter.hit(scope)
                if retry is not None:
                    await _send_json(send_wrapper, 429, {"detail": TOO_MANY},
                                     [(b"retry-after", str(retry).encode())])
                    return
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


async def _send_json(send: Callable, status_code: int, content: dict[str, Any],
                     headers: list[tuple[bytes, bytes]] | None = None) -> None:
    payload = json.dumps(content, ensure_ascii=False).encode("utf-8")
    await send({"type": "http.response.start", "status": status_code,
                "headers": [(b"content-type", b"application/json"),
                            (b"content-length", str(len(payload)).encode()),
                            *(headers or [])]})
    await send({"type": "http.response.body", "body": payload})


RATE_MAX_CLIENTS = 10_000  # counters kept at most; the least recently seen go first
TOO_MANY = "too many requests"  # the 429 detail: no echo of anything the client sent
RATE_LIMITED_PATH = "/api/recommend"


class RateLimiter:
    """Per-client counters of POST /api/recommend: a minute window and the Astana day.

    The client is client.host, or with `trusted_hops` = N proxies in front the N-th entry of
    X-Forwarded-For from the right — the one the last trusted proxy added, so an entry the
    client wrote itself does not help; with 0 the header is never read. Counters are keyed by
    sha256(salt + ip), the salt is random per process, so neither the log nor the memory holds
    an address; at most `max_clients` of them (least recently seen dropped first)."""

    def __init__(self, per_min: int, per_day: int, trusted_hops: int = 0, *,
                 clock: Callable[[], datetime] | None = None,
                 max_clients: int = RATE_MAX_CLIENTS) -> None:
        self.per_min = max(0, per_min)
        self.per_day = max(0, per_day)
        self.trusted_hops = max(0, trusted_hops)
        self.max_clients = max_clients
        self._clock = clock or (lambda: datetime.now(ASTANA_TZ))
        self._salt = secrets.token_bytes(16)
        # key -> [minute window start (unix s), count in it, Astana date, count that day]
        self._clients: OrderedDict[str, list[Any]] = OrderedDict()
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return self.per_min > 0 or self.per_day > 0

    def __len__(self) -> int:
        return len(self._clients)

    def client_ip(self, scope: dict) -> str:
        host = str((scope.get("client") or ("-", 0))[0])
        if self.trusted_hops <= 0:
            return host  # no trusted proxy: X-Forwarded-For is whatever the client wrote
        values = [v.decode("latin-1") for k, v in scope.get("headers", [])
                  if k.lower() == b"x-forwarded-for"]
        entries = [e.strip() for v in values for e in v.split(",") if e.strip()]
        return entries[-self.trusted_hops] if len(entries) >= self.trusted_hops else host

    def _key(self, ip: str) -> str:
        return hashlib.sha256(self._salt + ip.encode("utf-8", "replace")).hexdigest()[:32]

    def hit(self, scope: dict) -> int | None:
        """Count this request: None when it may go on, else the Retry-After seconds."""
        now = self._clock().astimezone(ASTANA_TZ)
        ts, day = now.timestamp(), now.date()
        key = self._key(self.client_ip(scope))
        with self._lock:
            rec = self._clients.pop(key, None) or [ts, 0, day, 0]
            if ts - rec[0] >= 60:
                rec[0], rec[1] = ts, 0
            if rec[2] != day:
                rec[2], rec[3] = day, 0
            retry: int | None = None
            if self.per_day and rec[3] >= self.per_day:  # until the Astana midnight
                midnight = datetime.combine(day + timedelta(days=1), dt_time(0), ASTANA_TZ)
                retry = math.ceil((midnight - now).total_seconds())
            elif self.per_min and rec[1] >= self.per_min:
                retry = math.ceil(60 - (ts - rec[0]))
            else:
                rec[1] += 1
                rec[3] += 1
            self._clients[key] = rec  # the most recently seen at the end
            while len(self._clients) > self.max_clients:
                self._clients.popitem(last=False)
        return None if retry is None else max(1, retry)


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


def _llm_budget(d: Deps | None) -> str:
    """The daily Claude budget (T161): none | ok | reached | misconfigured — never a sum."""
    fn = getattr(d.llm, "budget_state", None) if d is not None and d.llm is not None else None
    if fn is not None:
        return str(fn())
    state = (d.settings if d is not None else get_settings()).llm_budget_state
    return {"set": "ok"}.get(state, state)


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


def _city_gaps_path(app: FastAPI) -> Path | None:
    """settings.city_gaps_path (QADAM_CITY_GAPS_PATH) of the app's deps, else of the env."""
    deps: Deps | None = app.state.deps
    settings = deps.settings if deps is not None else get_settings()
    return getattr(settings, "city_gaps_path", None)


def _city_gaps_state(request: Request) -> str:
    return "ready" if request.app.state.city_gaps is not None else "missing"


def start_weather_warm_up(deps: Deps) -> threading.Thread:
    """T133: today's weather for the landmark areas, loaded in the background at start, so
    requests rarely wait for Open-Meteo (they never wait longer than weather.TIMEOUT_S).
    Public landmark points only; the server starts without waiting for it."""
    points = [lm.geo for lm in deps.store.landmarks.values()]
    thread = threading.Thread(target=weather.warm_up, args=(points,), name="weather-warm-up",
                              daemon=True)
    thread.start()
    return thread


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
            else:
                try:
                    start_weather_warm_up(app.state.deps)
                except Exception:  # noqa: BLE001 - the weather is optional (rule 5)
                    log.exception("weather warm-up did not start; requests ask on their own")
        # T142: the city gaps map is read and checked once, whatever the store did
        app.state.city_gaps = await run_in_threadpool(city_gaps.load, _city_gaps_path(app))
        yield

    app = FastAPI(title="Qadam API", version=SCHEMA_VERSION, lifespan=lifespan)
    app.state.deps = deps
    app.state.city_gaps = None  # bytes of city_gaps.json once the lifespan loaded it
    app.state.prompt_problems = check_prompts()
    llm_slots = threading.BoundedSemaphore(MAX_CONCURRENT_LLM)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=DEV_ORIGINS,
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type"],
    )
    limits = deps.settings if deps is not None else get_settings()
    limiter = RateLimiter(limits.rate_per_min, limits.rate_per_day, limits.trusted_proxy_hops)
    app.state.rate_limiter = limiter
    app.add_middleware(ApiGuard, limiter=limiter)

    @app.exception_handler(RequestValidationError)
    async def request_invalid(request: Request, exc: RequestValidationError) -> JSONResponse:
        # input/ctx may contain private data or NaN; even loc/msg can echo user input.
        # Do not serialize or log the errors themselves, or let their serialization fail.
        log.warning("request validation failed (%d errors)", len(exc.errors()))
        return JSONResponse(status_code=422, content={"detail": [{
            "type": "request_validation",
            "loc": ["body"],
            "msg": "Неверный формат запроса.",
        }]})

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
                         "llm": "off", "llm_budget": _llm_budget(None),
                         "city_gaps": _city_gaps_state(request),
                         "schema_version": SCHEMA_VERSION},
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
            "llm_budget": _llm_budget(d),  # none | ok | reached | misconfigured, no sums
            "warnings": [*problems, *d.store.warnings],
            "city_gaps": _city_gaps_state(request),  # does not change `status` (contract 1.4.0)
            "schema_version": SCHEMA_VERSION,
        })

    @app.get("/api/city-gaps", response_model=CityGaps, tags=["api"],
             summary="Карта пробелов данных: агрегаты открытых данных по ячейкам H3 r8",
             responses={503: {"description": "Файл карты не собран или не проходит схему",
                              "content": {"application/json": {"example": {
                                  "detail": city_gaps.UNAVAILABLE}}}}})
    async def city_gaps_map(request: Request) -> Response:
        """The file as is, the same for everyone: no parameters (extra ones are ignored and
        never reach the answer or the log line, which has only the path, code and time)."""
        body: bytes | None = request.app.state.city_gaps
        if body is None:
            return JSONResponse(status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                                content={"detail": city_gaps.UNAVAILABLE})
        return Response(content=body, media_type="application/json",
                        headers={"Cache-Control": city_gaps.CACHE_CONTROL})

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
