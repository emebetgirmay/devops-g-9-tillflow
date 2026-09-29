"""Prometheus metrics for POS — ADR 0010 section 2, work item P-1.

Scraped by the ADOT sidecar at GET /metrics (ADR 0009 G3-7's scrape path,
shared with Payments). Names, types and labels here are the accepted
contract; renaming or adding a label needs the Reliability DRI's sign-off
per the ADR.

Hard rule, enforced by review not code: no tenant, sale, payment, attendant,
phone or trace id in any label. Those belong in logs, not metrics — an ID in
a label turns into a new time series per ID, which is an unbounded-cardinality
outage waiting to happen. `route` is always the route *template*
("/tenants/{tenant_id}/sales"), never the resolved path.
"""

from __future__ import annotations

from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, Counter, Histogram, generate_latest

REGISTRY = CollectorRegistry()

# Routes excluded from all HTTP metrics below: operational endpoints, not
# product surface. ADR 0010 section 3: "/health and /ready are excluded from
# SLI math" — simplest way to honor that is to never emit them at all, and
# /metrics/scraping itself would be self-referential noise either way.
EXCLUDED_ROUTES = frozenset({"/health", "/ready", "/version", "/metrics"})

http_requests_total = Counter(
    "pos_http_requests_total",
    "HTTP requests handled by POS, excluding /health, /ready, /version, /metrics.",
    ["route", "status_class"],
    registry=REGISTRY,
)

http_request_duration_seconds = Histogram(
    "pos_http_request_duration_seconds",
    "HTTP request duration in seconds, by route template.",
    ["route"],
    registry=REGISTRY,
)

sale_creates_total = Counter(
    "pos_sale_creates_total",
    "POST /tenants/{tenant_id}/sales outcomes.",
    ["result"],  # created | replayed | conflict | invalid
    registry=REGISTRY,
)

payment_events_total = Counter(
    "pos_payment_events_total",
    "Payment outcome events applied via app/payment_outcomes.py, from either "
    "the on-demand payment-reconcile endpoint or the background scheduler.",
    ["result"],  # applied | replay | rejected
    registry=REGISTRY,
)

sales_paid_total = Counter(
    "pos_sales_paid_total",
    "Sales that reached PAID for the first time. No labels — see the module docstring.",
    registry=REGISTRY,
)


def record_request(route: str, status_code: int, duration_seconds: float) -> None:
    if route in EXCLUDED_ROUTES:
        return
    status_class = f"{status_code // 100}xx"
    http_requests_total.labels(route=route, status_class=status_class).inc()
    http_request_duration_seconds.labels(route=route).observe(duration_seconds)


def record_sale_create(result: str) -> None:
    sale_creates_total.labels(result=result).inc()


def record_payment_event(result: str) -> None:
    payment_events_total.labels(result=result).inc()


def record_sale_paid() -> None:
    sales_paid_total.inc()


def render_latest() -> bytes:
    return generate_latest(REGISTRY)


METRICS_CONTENT_TYPE = CONTENT_TYPE_LATEST


def reset_for_tests() -> None:
    """Metric objects are module-level globals, so counts persist across the
    whole pytest process unless cleared — tests/conftest.py's autouse
    fixture calls this before every test.

    ``.clear()`` only drops labelled children (created via ``.labels(...)``),
    which covers every metric here except ``sales_paid_total`` — it has no
    labels, so its value lives directly on the root collector and needs the
    private ``_value`` reset instead.
    """
    for metric in (http_requests_total, http_request_duration_seconds, sale_creates_total, payment_events_total):
        metric.clear()
    sales_paid_total._value.set(0)  # noqa: SLF001 — no public reset for an unlabelled counter
