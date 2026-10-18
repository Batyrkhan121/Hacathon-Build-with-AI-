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
"""

from __future__ import annotations

from fastapi import FastAPI

from app.schemas import SCHEMA_VERSION

app = FastAPI(title="Qadam API", version=SCHEMA_VERSION)


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "stub", "schema_version": SCHEMA_VERSION}
