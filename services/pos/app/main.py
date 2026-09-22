"""TillFlow POS API.

/health, /ready, /version keep the exact response shapes the G1 stub used
(infra/envs/sandbox/ecs.tf's container healthcheck and
evidence/platform-delivery/collect.sh both depend on them).
"""

from __future__ import annotations

import os
import urllib.error
import urllib.request
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from .db import init_db
from .routers import catalog, commission, internal, sales

COMMIT_SHA = os.environ.get("COMMIT_SHA", "local")
IMAGE_DIGEST = os.environ.get("IMAGE_DIGEST", "unknown")
ADOT_HEALTH_URL = os.environ.get("ADOT_HEALTH_URL", "http://127.0.0.1:13133/")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title="TillFlow POS API", version=COMMIT_SHA, lifespan=lifespan)


def _adot_healthy() -> bool:
    try:
        with urllib.request.urlopen(ADOT_HEALTH_URL, timeout=2) as resp:  # noqa: S310
            return 200 <= resp.status < 300
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "service": "pos"}


@app.get("/ready", response_model=None)
def ready() -> JSONResponse | dict:
    if not _adot_healthy():
        return JSONResponse(
            status_code=503,
            content={"status": "not_ready", "service": "pos", "reason": "adot_unhealthy"},
        )
    return {"status": "ready", "service": "pos"}


@app.get("/version")
def version() -> dict:
    return {"service": "pos", "commit": COMMIT_SHA, "image_digest": IMAGE_DIGEST}


app.include_router(catalog.router)
app.include_router(sales.router)
app.include_router(internal.router)
app.include_router(commission.router)
