"""HTTP API + static web UI. Owner: LLM lane (task T34).

Endpoints (contract):
  GET  /api/health     -> {"status": "ok", "poi_count": int, "index": "ready|lexical",
                           "llm": "ok|off"}
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
- Handlers are sync `def`: the pipeline is blocking (SDK calls, numpy), FastAPI runs it in the
  threadpool.
- An unexpected error in /api/recommend is logged by type and stack frames only: exception
  messages may contain the request body (query + cell), which must never reach the logs.
"""

from __future__ import annotations

import logging
import time
import traceback
from collections import Counter
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app.config import ROOT, get_settings
from app.llm import LLMClient
from app.pipeline import Deps, InvalidRequest, recommend
from app.schemas import SCHEMA_VERSION, Answer, Landmark, RecommendRequest
from app.store import load_store

log = logging.getLogger("qadam.api")

WEB_DIR = ROOT / "web"
# The WEB lane serves web/ with `python -m http.server 5500` while developing.
DEV_ORIGINS = ["http://localhost:5500", "http://127.0.0.1:5500"]


def build_default_deps() -> Deps:
    settings = get_settings()
    return Deps(store=load_store(settings), llm=LLMClient(settings), settings=settings)


def get_deps(request: Request) -> Deps:
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
            except Exception as exc:  # noqa: BLE001 - serve the UI and a clear 503 instead
                log.error("store failed to load (%s); data endpoints answer 503",
                          type(exc).__name__)
        yield

    app = FastAPI(title="Qadam API", version=SCHEMA_VERSION, lifespan=lifespan)
    app.state.deps = deps
    app.add_middleware(
        CORSMiddleware,
        allow_origins=DEV_ORIGINS,
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type"],
    )

    @app.middleware("http")
    async def access_log(request: Request, call_next: Any) -> Any:
        started = time.perf_counter()
        response = await call_next(request)
        if request.url.path.startswith("/api/"):
            log.info("%s %s -> %d in %.0f ms", request.method, request.url.path,
                     response.status_code, (time.perf_counter() - started) * 1000)
        return response

    @app.get("/api/health")
    def health(request: Request) -> JSONResponse:
        d: Deps | None = request.app.state.deps
        if d is None:
            return JSONResponse(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                content={"status": "unavailable", "poi_count": 0, "index": "lexical",
                         "llm": "off", "schema_version": SCHEMA_VERSION},
            )
        return JSONResponse({
            "status": "ok",
            "poi_count": len(d.store.pois),
            "index": "ready" if d.store.embeddings is not None else "lexical",
            "llm": "ok" if d.llm is not None and d.llm.available else "off",
            "schema_version": SCHEMA_VERSION,
        })

    @app.get("/api/landmarks", response_model=list[Landmark])
    def landmarks(d: DepsDep) -> list[Landmark]:
        return sorted(d.store.landmarks.values(), key=lambda lm: lm.id)

    @app.get("/api/meta")
    def meta(d: DepsDep) -> dict[str, Any]:
        pois = d.store.pois
        return {
            "snapshot_date": d.store.snapshot_date,
            "poi_count": len(pois),
            "by_category": dict(sorted(Counter(p.category for p in pois).items())),
            "sources": sorted({s for p in pois for s in p.sources}),
            "index": "ready" if d.store.embeddings is not None else "lexical",
            "schema_version": SCHEMA_VERSION,
        }

    @app.post("/api/recommend", response_model=Answer)
    def recommend_endpoint(req: RecommendRequest, d: DepsDep) -> Answer:
        try:
            return recommend(req, d)
        except InvalidRequest as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except Exception as exc:  # noqa: BLE001 - see the module note on logging
            # Caught here, not by a global handler: Starlette re-raises from its error
            # middleware and the server would log the message (it may echo the request).
            frames = "".join(traceback.format_tb(exc.__traceback__)[-4:])
            log.error("recommend failed: %s\n%s", type(exc).__name__, frames)
            raise HTTPException(status_code=500, detail="Внутренняя ошибка сервера.") from None

    if WEB_DIR.is_dir():  # last, so /api/* routes win
        app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
    return app


app = create_app()
