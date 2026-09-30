"""TillFlow POS API.

/health, /ready, /version keep the exact response shapes the G1 stub used
(infra/envs/sandbox/ecs.tf's container healthcheck and
evidence/platform-delivery/collect.sh both depend on them).
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import urllib.error
import urllib.request
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

from . import metrics, scheduler, tracing
from .db import init_db
from .routers import catalog, commission, internal, sales

COMMIT_SHA = os.environ.get("COMMIT_SHA", "local")
IMAGE_DIGEST = os.environ.get("IMAGE_DIGEST", "unknown")
ADOT_HEALTH_URL = os.environ.get("ADOT_HEALTH_URL", "http://127.0.0.1:13133/")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    reconcile_task = asyncio.create_task(scheduler.reconcile_loop()) if scheduler.is_enabled() else None
    yield
    if reconcile_task is not None:
        reconcile_task.cancel()


app = FastAPI(title="TillFlow POS API", version=COMMIT_SHA, lifespan=lifespan)


# Probes and the scrape run every few seconds; logging them would bury the real requests.
_UNLOGGED_ROUTES = {"/health", "/ready", "/metrics"}


@app.middleware("http")
async def record_http_metrics(request: Request, call_next):
    trace_id = tracing.start(request.headers.get("traceparent"))
    start = time.perf_counter()
    response = await call_next(request)
    duration = time.perf_counter() - start
    # request.scope["route"] is set by Starlette's router once a match is
    # found, and always carries the route *template* (e.g.
    # "/tenants/{tenant_id}/sales"), never the resolved path — exactly the
    # label ADR 0010 requires. An unmatched path (404, or someone probing
    # arbitrary URLs) has no route; bucket those under a fixed label rather
    # than the raw path, which would otherwise be unbounded cardinality.
    route = request.scope.get("route")
    route_path = route.path if route is not None else "unmatched"
    metrics.record_request(route_path, response.status_code, duration)
    response.headers["X-Trace-Id"] = trace_id
    if route_path not in _UNLOGGED_ROUTES:
        # Same shape as Payments' request line (ADR 0009 section 5): the route template, never
        # the resolved path, and no body, phone number or tenant data.
        line = {
            "ts": time.time(),
            "level": "INFO",
            "service": "pos",
            "event": "request",
            "trace_id": trace_id,
            "result": f"{request.method} {route_path} -> {response.status_code}",
        }
        print(json.dumps(line, sort_keys=True), flush=True)
    return response


@app.get("/metrics", response_model=None)
def metrics_endpoint() -> Response:
    return Response(content=metrics.render_latest(), media_type=metrics.METRICS_CONTENT_TYPE)


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
