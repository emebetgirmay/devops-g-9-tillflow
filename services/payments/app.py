#!/usr/bin/env python3
"""Payments API for TillFlow (G2): idempotent STK charges and B2C payouts over the M-Pesa ports.

Only the FakeAdapter is wired in this build (ADR 0004): nothing here talks to Safaricom, and
MPESA_ADAPTER=daraja_sandbox is refused at startup. Commission calls /payouts only.
"""

from __future__ import annotations

import json
import re
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

_SHARED = Path(__file__).resolve().parent.parent / "_shared"
if _SHARED.is_dir():  # repo layout; in the image the mpesa package sits next to this file
    sys.path.insert(0, str(_SHARED))

from mpesa import FakeAdapter, ManualClock

from core.common import Reply
from core.config import ConfigError, Settings, SystemClock
from core.payments import PaymentService
from core.payouts import PayoutService
from core.store import Store

MAX_BODY_BYTES = 64 * 1024
ID_PATH = r"([A-Za-z0-9_.:-]{1,80})"


class App:
    """Wires settings, storage, the adapter and the services. Handlers stay thin."""

    def __init__(self, settings: Settings, clock=None, adapter=None) -> None:
        self.settings = settings
        if clock is None:
            clock = ManualClock(time.time()) if settings.fake_clock == "manual" else SystemClock()
        self.clock = clock
        self.adapter = adapter if adapter is not None else FakeAdapter(clock=clock)
        self.store = Store(settings.db_path)
        self.payments = PaymentService(settings, self.store, self.adapter, clock)
        self.payouts = PayoutService(settings, self.store, self.adapter, clock)

    # Routing -------------------------------------------------------------------------------

    def dispatch(
        self, method: str, path: str, headers: dict[str, str], body: bytes, remote_addr: str
    ) -> Reply:
        path = path.split("?", 1)[0].rstrip("/") or "/"
        lower = {k.lower(): v for k, v in headers.items()}
        if method == "GET":
            return self._get(path)
        if method == "POST":
            return self._post(path, lower, headers, body, remote_addr)
        return Reply(405, {"error": "method_not_allowed"})

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
        match = re.fullmatch(rf"/payments/{ID_PATH}", path)
        if match:
            return self.payments.get_payment(match.group(1))
        match = re.fullmatch(rf"/payouts/{ID_PATH}", path)
        if match:
            return self.payouts.get_payout(match.group(1))
        return Reply(404, {"error": "not_found"})

    def _post(
        self, path: str, lower: dict[str, str], headers: dict[str, str], body: bytes, remote: str
    ) -> Reply:
        if path == "/payments/daraja/callback":
            return self.payments.handle_callback(headers, body, remote)
        if path == "/payments/daraja/b2c-callback":
            return self.payouts.handle_result(headers, body, remote)
        if path == "/_admin/sweep":
            return Reply(200, {"payments": self.payments.sweep(), "payouts": self.payouts.sweep()})
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

    # Fake-adapter driver (test and k6 only; this build has no other adapter) ---------------

    def _fake(self, path: str, body: bytes) -> Reply:
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
            payload = json.dumps(reply.body).encode("utf-8")
            self.send_response(reply.status)
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
    app = App(settings)
    server = ThreadingHTTPServer(("0.0.0.0", settings.port), make_handler(app))
    print(f"payments listening on {settings.port} (adapter={settings.adapter})", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
