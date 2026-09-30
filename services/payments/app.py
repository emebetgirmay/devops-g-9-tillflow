#!/usr/bin/env python3
"""Payments API for TillFlow (G2): idempotent STK charges and B2C payouts over the M-Pesa ports.

The FakeAdapter is the default and the only adapter CI, tests and k6 use (ADR 0004).
MPESA_ADAPTER=daraja_sandbox selects core/daraja_sandbox.py (B2C only) in the deployed sandbox, and
refuses to start without its Platform-managed settings. Commission calls /payouts only.
"""

from __future__ import annotations

import json
import re
import sys
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

_SHARED = Path(__file__).resolve().parent.parent / "_shared"
if _SHARED.is_dir():  # repo layout; in the image the mpesa package sits next to this file
    sys.path.insert(0, str(_SHARED))

from mpesa import FakeAdapter, ManualClock

from core import jsonlog, metrics, otlp, tracing
from core.common import Reply
from core.config import ConfigError, Settings, SystemClock
from core.daraja_sandbox import DarajaSandboxAdapter
from core.payments import PaymentService
from core.payouts import PayoutService
from core.store import Store

MAX_BODY_BYTES = 64 * 1024
ID_PATH = r"([A-Za-z0-9_.:-]{1,80})"

# RED metrics (ADR 0009 section 2) label every request by its route *template*
# (e.g. "/payments/{id}"), never the resolved path — a raw path would make
# payments_http_requests_total grow one time series per payment_id ever
# created, which is exactly the unbounded-cardinality mistake the ADR warns
# about. This list has to be kept in the same shape as the routing in _get/
# _post below; there's no framework here to derive it automatically.
_EXCLUDED_ROUTES = frozenset({"/health", "/ready", "/version", "/metrics"})

# Set by API Gateway on every request it forwards (infra/envs/sandbox/api_gateway.tf); its
# presence means "came from the internet". See App._blocked_at_public_edge.
PUBLIC_EDGE_HEADER = "x-tillflow-edge"
_ROUTE_PATTERNS: tuple[tuple[str, re.Pattern], ...] = (
    ("/payments/{id}/reconcile", re.compile(rf"^/payments/{ID_PATH}/reconcile$")),
    ("/payouts/{id}/reconcile", re.compile(rf"^/payouts/{ID_PATH}/reconcile$")),
    ("/payments/{id}", re.compile(rf"^/payments/{ID_PATH}$")),
    ("/payouts/{id}", re.compile(rf"^/payouts/{ID_PATH}$")),
    ("/payments", re.compile(r"^/payments$")),
    ("/payouts", re.compile(r"^/payouts$")),
    ("/payments/daraja/callback", re.compile(r"^/payments/daraja/callback$")),
    ("/payments/daraja/b2c-callback", re.compile(r"^/payments/daraja/b2c-callback$")),
    ("/_admin/sweep", re.compile(r"^/_admin/sweep$")),
    ("/_admin/invariants", re.compile(r"^/_admin/invariants$")),
    ("/_fake/advance", re.compile(r"^/_fake/advance$")),
    ("/_fake/deliver-callbacks", re.compile(r"^/_fake/deliver-callbacks$")),
    ("/_fake/script-result-code", re.compile(r"^/_fake/script-result-code$")),
    ("/health", re.compile(r"^/health$")),
    ("/ready", re.compile(r"^/ready$")),
    ("/version", re.compile(r"^/version$")),
    ("/metrics", re.compile(r"^/metrics$")),
)


def _route_label(path: str) -> str:
    for label, pattern in _ROUTE_PATTERNS:
        if pattern.fullmatch(path):
            return label
    return "unmatched"


class App:
    """Wires settings, storage, the adapter and the services. Handlers stay thin."""

    def __init__(self, settings: Settings, clock=None, adapter=None) -> None:
        self.settings = settings
        if clock is None:
            clock = ManualClock(time.time()) if settings.fake_clock == "manual" else SystemClock()
        self.clock = clock
        if adapter is None:
            adapter = (
                DarajaSandboxAdapter(settings.daraja)
                if settings.daraja is not None
                else FakeAdapter(clock=clock)
            )
        self.adapter = adapter
        self.store = Store(settings.database_url or settings.db_path, settings.db_pool_size)
        self.payments = PaymentService(settings, self.store, self.adapter, clock)
        self.payouts = PayoutService(settings, self.store, self.adapter, clock)

    # Routing -------------------------------------------------------------------------------

    def dispatch(
        self, method: str, path: str, headers: dict[str, str], body: bytes, remote_addr: str
    ) -> Reply:
        path = path.split("?", 1)[0].rstrip("/") or "/"
        route = _route_label(path)
        lower = {k.lower(): v for k, v in headers.items()}
        started = time.time()
        with tracing.trace_context(lower.get("traceparent")) as trace_id:
            with metrics.Timer() as timer:
                try:
                    remote_addr = self._source(lower, remote_addr)
                    if self._blocked_at_public_edge(path, lower):
                        reply = Reply(404, {"error": "not_found"})
                    elif method == "GET":
                        reply = self._get(path)
                    elif method == "POST":
                        reply = self._post(path, lower, headers, body, remote_addr)
                    else:
                        reply = Reply(405, {"error": "method_not_allowed"})
                except Exception as exc:  # noqa: BLE001 - last line of defence, see below
                    # An exception escaping here used to kill the handler thread with no reply:
                    # the ALB answered 502, and nothing reached the 5xx metric, the request log
                    # or the burn alarms (G3 k6 soak, 110 silent 502s). Answer a counted, logged
                    # 500 instead. Money stays safe: every write is idempotent, so a caller's
                    # retry with the same Idempotency-Key replays rather than repeats.
                    jsonlog.log_line(
                        level="ERROR",
                        service="payments",
                        event="unhandled_error",
                        trace_id=trace_id,
                        result=f"{method} {route}: {type(exc).__name__}",
                    )
                    traceback.print_exc(file=sys.stderr)
                    reply = Reply(500, {"error": "internal_error", "trace_id": trace_id})
            if route not in _EXCLUDED_ROUTES:
                status_class = f"{reply.status // 100}xx"
                metrics.http_requests_total.inc(route, status_class)
                metrics.http_request_duration_seconds.observe(timer.elapsed, route)
            reply.headers.setdefault("X-Trace-Id", trace_id)
            jsonlog.log_line(
                level="INFO",
                service="payments",
                event="request",
                trace_id=trace_id,
                result=f"{method} {route} -> {reply.status}",
            )
            if route not in _EXCLUDED_ROUTES:
                otlp.export(
                    trace_id=trace_id,
                    span_id=tracing.current_span_id(),
                    parent_span_id=tracing.current_parent_span_id(),
                    name=f"{method} {route}",
                    start=started,
                    end=time.time(),
                    attributes={
                        "http.method": method,
                        "http.route": route,
                        "http.status_code": reply.status,
                    },
                    error=reply.status >= 500,
                )
        return reply

    def _blocked_at_public_edge(self, path: str, lower: dict[str, str]) -> bool:
        """Operator and test paths are not served to the internet on a real-adapter build.

        API Gateway overwrites PUBLIC_EDGE_HEADER on every request it forwards, so a caller can
        neither forge nor strip it; in-VPC callers (the scheduled sweep, k6) never pass through
        API Gateway and never carry it. /_fake/* and /_admin/invariants already answer 404 unless
        the FakeAdapter is running; this also closes /_admin/sweep. The sandbox runs the
        FakeAdapter, so drills and demos that call these paths through the public URL still work
        there (ADR 0009 G3-7)."""
        if not path.startswith(("/_admin/", "/_fake/")):
            return False
        return PUBLIC_EDGE_HEADER in lower and not isinstance(self.adapter, FakeAdapter)

    def _source(self, lower: dict[str, str], peer: str) -> str:
        """The caller's address for the callback allowlist. Behind API Gateway the socket peer is
        the ALB, and HTTP APIs reserve X-Forwarded-For, so the edge overwrites our own header
        with $context.identity.sourceIp; whatever a caller sent in it is replaced. A missing
        header means we cannot tell: "" (never allowlisted)."""
        name = self.settings.callback_source_header
        return lower.get(name, "").strip() if name else peer

    def _get(self, path: str) -> Reply:
        if path == "/health":
            return Reply(200, {"status": "ok", "service": "payments"})
        if path == "/ready":
            if not self.store.ping():
                return Reply(
                    503, {"status": "not_ready", "service": "payments", "reason": "db_unavailable"}
                )
            return Reply(200, {"status": "ready", "service": "payments"})
        if path == "/version":
            return Reply(
                200,
                {
                    "service": "payments",
                    "commit": self.settings.commit_sha,
                    "image_digest": self.settings.image_digest,
                },
            )
        if path == "/metrics":
            # Not meant to be internet-reachable (ADR 0009 open question 3) — kept off the
            # public API Gateway/ALB routes; that's Platform's routing, not this app's job to
            # enforce. Scraped by the ADOT sidecar at 127.0.0.1:<port>/metrics.
            with self.store.connection() as conn:
                payload = metrics.render(
                    conn, now=self.clock.now(), payouts_enabled_default=self.settings.payouts_enabled
                )
            return Reply(200, payload, {"Content-Type": metrics.CONTENT_TYPE})
        if path == "/_admin/invariants":
            return self._invariants()
        match = re.fullmatch(rf"/payments/{ID_PATH}", path)
        if match:
            return self.payments.get_payment(match.group(1))
        match = re.fullmatch(rf"/payouts/{ID_PATH}", path)
        if match:
            return self.payouts.get_payout(match.group(1))
        return Reply(404, {"error": "not_found"})

    def _invariants(self) -> Reply:
        """ADR 0009 section 6 / G3-3: the counts k6's soak run asserts stay
        at zero throughout. Fake-adapter builds only — this is a test/k6
        harness endpoint, not a production API, so it's gated exactly like
        /_fake/* below rather than always being reachable."""
        if not isinstance(self.adapter, FakeAdapter):
            return Reply(404, {"error": "not_found"})
        with self.store.connection() as conn:
            succeeded_payments = conn.execute(
                "SELECT COUNT(*) AS n FROM payments WHERE state = 'SUCCEEDED'"
            ).fetchone()["n"]
            payment_credits = conn.execute(
                "SELECT COUNT(*) AS n FROM ledger_entries WHERE entry_type = 'PAYMENT_CREDIT'"
            ).fetchone()["n"]
            duplicate_ledger_entries = conn.execute(
                "SELECT COUNT(*) AS n FROM ("
                " SELECT 1 FROM ledger_entries GROUP BY provider, provider_ref, entry_type"
                " HAVING COUNT(*) > 1)"
            ).fetchone()["n"]
            payout_keys_with_multiple_live_disbursements = conn.execute(
                "SELECT COUNT(*) AS n FROM ("
                " SELECT 1 FROM disbursements WHERE state <> 'FAILED' GROUP BY payout_key"
                " HAVING COUNT(*) > 1)"
            ).fetchone()["n"]
            # REJECTED_AT_INITIATION only happens synchronously, at creation, on the
            # branch _finish_create takes when the adapter definitively declines —
            # unknown_since is only ever set on the other branch (an initiate timeout).
            # They're mutually exclusive by construction; a payment with both means
            # that guarantee broke.
            payments_declined_by_a_timeout = conn.execute(
                "SELECT COUNT(*) AS n FROM payments"
                " WHERE decline_reason = 'REJECTED_AT_INITIATION' AND unknown_since IS NOT NULL"
            ).fetchone()["n"]
        return Reply(
            200,
            {
                "credits_equal_succeeded_payments": succeeded_payments == payment_credits,
                "succeeded_payments": succeeded_payments,
                "payment_credits": payment_credits,
                "duplicate_ledger_entries": duplicate_ledger_entries,
                "payout_keys_with_multiple_live_disbursements": payout_keys_with_multiple_live_disbursements,
                "payments_declined_by_a_timeout": payments_declined_by_a_timeout,
            },
        )

    def _post(
        self, path: str, lower: dict[str, str], headers: dict[str, str], body: bytes, remote: str
    ) -> Reply:
        if path == "/payments/daraja/callback":
            return self.payments.handle_callback(headers, body, remote)
        if path == "/payments/daraja/b2c-callback":
            return self.payouts.handle_result(headers, body, remote)
        if path == "/_admin/sweep":
            return Reply(200, self.run_reconcile_pass())
        if path.startswith("/_fake/"):
            return self._fake(path, body)
        match = re.fullmatch(rf"/payments/{ID_PATH}/reconcile", path)
        if match:
            return self.payments.reconcile_payment(match.group(1))
        match = re.fullmatch(rf"/payouts/{ID_PATH}/reconcile", path)
        if match:
            return self.payouts.reconcile_payout(match.group(1))
        if path in ("/payments", "/payouts"):
            try:
                payload = json.loads(body) if body else None
            except ValueError:
                return Reply(400, {"error": "invalid_json"})
            key = lower.get("idempotency-key")
            if path == "/payments":
                return self.payments.create_payment(key, payload)
            return self.payouts.create_payout(key, payload)
        return Reply(404, {"error": "not_found"})

    def run_reconcile_pass(self) -> dict:
        """One reconcile pass: both services' sweep(). The single choke point both
        POST /_admin/sweep and reconcile.py's run_in_process go through, so
        payments_reconcile_runs_total and payments_reconcile_last_success_timestamp_seconds
        mean the same thing no matter which path triggered the pass."""
        try:
            result = {"payments": self.payments.sweep(), "payouts": self.payouts.sweep()}
        except Exception:
            metrics.reconcile_runs_total.inc("error")
            raise
        metrics.reconcile_runs_total.inc("ok")
        with self.store.tx() as conn:
            now = self.clock.now()
            self.store.set_flag(conn, "reconcile_last_success", True, "sweep completed", now)
        return result

    # Fake-adapter driver (test and k6 only; this build has no other adapter) ---------------

    def _fake(self, path: str, body: bytes) -> Reply:
        if not isinstance(self.adapter, FakeAdapter):
            return Reply(404, {"error": "not_found"})
        try:
            data = json.loads(body) if body else {}
        except ValueError:
            return Reply(400, {"error": "invalid_json"})
        if not isinstance(data, dict):
            return Reply(400, {"error": "invalid_request"})
        if path == "/_fake/advance":
            seconds = data.get("seconds")
            if not isinstance(self.clock, ManualClock):
                return Reply(409, {"error": "clock_is_not_manual"})
            if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or seconds < 0:
                return Reply(400, {"error": "seconds must be a non-negative number"})
            self.clock.advance(seconds)
            return Reply(200, {"now": self.clock.now()})
        if path == "/_fake/deliver-callbacks":
            source = self.settings.callback_allowed_ips[0]
            delivered = []
            for item in self.adapter.due_callbacks():
                reply = self.payments.handle_callback(item.headers, item.body, source)
                delivered.append(
                    {"kind": "stk", "ref": item.provider_ref, "http": reply.status, **reply.body}
                )
            for item in self.adapter.due_disbursement_results():
                reply = self.payouts.handle_result(item.headers, item.body, source)
                delivered.append(
                    {"kind": "b2c", "ref": item.provider_ref, "http": reply.status, **reply.body}
                )
            return Reply(200, {"delivered": delivered})
        if path == "/_fake/script-result-code":
            return self._script_code(data)
        return Reply(404, {"error": "not_found"})

    def _script_code(self, data: dict) -> Reply:
        kind, ident, delay = data.get("kind"), data.get("id"), data.get("delay_s", 0)
        if kind not in ("payment", "payout") or not isinstance(ident, str) or "code" not in data:
            return Reply(400, {"error": "need kind (payment|payout), id and code"})
        try:
            with self.store.connection() as conn:
                if kind == "payment":
                    row = self.payments._find(conn, ident)
                    ref = row["provider_ref"] if row else None
                else:
                    row = self.payouts._find(conn, ident)
                    ref = row["originator_conversation_id"] if row else None
            if ref is None:
                return Reply(404, {"error": "not_found_or_no_provider_reference"})
            if kind == "payment":
                self.adapter.deliver_result_code(ref, data["code"], delay)
            else:
                self.adapter.deliver_disbursement_code(ref, data["code"], delay)
        except (ValueError, TypeError) as exc:
            return Reply(400, {"error": str(exc)})
        return Reply(202, {"scheduled": True})


def make_handler(app: App) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args) -> None:
            sys.stderr.write(f"{self.address_string()} - {fmt % args}\n")

        def _handle(self, method: str) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY_BYTES:
                self._send(Reply(413, {"error": "body_too_large"}))
                return
            body = self.rfile.read(length) if length else b""
            reply = app.dispatch(
                method, self.path, dict(self.headers.items()), body, self.client_address[0]
            )
            self._send(reply)

        def _send(self, reply: Reply) -> None:
            # Every Reply.body is a JSON dict except GET /metrics, which is raw
            # Prometheus text — see core/common.py's Reply docstring.
            if isinstance(reply.body, (bytes, bytearray)):
                payload = bytes(reply.body)
            else:
                payload = json.dumps(reply.body).encode("utf-8")
            self.send_response(reply.status)
            if "Content-Type" not in reply.headers:
                self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            for name, value in reply.headers.items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self) -> None:
            self._handle("GET")

        def do_POST(self) -> None:
            self._handle("POST")

    return Handler


def main() -> None:
    try:
        settings = Settings.from_env()
    except ConfigError as exc:
        print(f"payments: refusing to start: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    try:
        app = App(settings)
    except ImportError as exc:  # a postgresql:// DATABASE_URL on an image without the driver
        print(f"payments: refusing to start: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    server = ThreadingHTTPServer(("0.0.0.0", settings.port), make_handler(app))
    print(f"payments listening on {settings.port} (adapter={settings.adapter})", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
