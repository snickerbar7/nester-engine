"""FastAPI entrypoint — one deployment, two contracts.

  service.harriet   frozen compat contract (camelCase NestResult):
                      GET  /health · POST /extract · POST /nest
  service.v1        neutral contract (snake_case, engine vocabulary):
                      GET  /v1/health · POST /v1/extract · POST /v1/nest

Both are thin adapters over `service.core`. Auth is per-client: a bearer token
resolves to a client id and, optionally, the object-key prefix that client is
scoped to (see `service.core.auth`).

Run:  uvicorn service.app:app --host 0.0.0.0 --port ${PORT:-8000}
"""

from __future__ import annotations

from fastapi import FastAPI

from .harriet.routes import router as harriet_router
from .v1.routes import router as v1_router

app = FastAPI(title="Nester Service", version="1.1.0")

app.include_router(harriet_router, tags=["harriet"])
app.include_router(v1_router, prefix="/v1", tags=["v1"])
