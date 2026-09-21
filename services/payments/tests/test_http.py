"""The real HTTP server, plus the startup refusals of the actual entry point."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from dataclasses import replace
from http.server import ThreadingHTTPServer
from pathlib import Path

import _bootstrap  # noqa: F401
from helpers import KEY
from mpesa import FakeAdapter, ManualClock

from app import App, make_handler
from core.config import Settings

SERVICE_DIR = Path(__file__).resolve().parent.parent


class ServerCase(unittest.TestCase):
    allowed_ips: tuple[str, ...] = ("127.0.0.1", "::1")

    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        settings = replace(
            Settings(),
            db_path=str(Path(self._dir.name) / "payments.db"),
            callback_allowed_ips=self.allowed_ips,
            commit_sha="abc123",
        )
        clock = ManualClock(1_000_000.0)
        self.app = App(settings, clock=clock, adapter=FakeAdapter(clock=clock))
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.app))
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def request(self, method: str, path: str, body=None, headers=None):
        data = (
            None
            if body is None
            else (body if isinstance(body, bytes) else json.dumps(body).encode())
        )
        req = urllib.request.Request(self.base + path, data=data, method=method)
        for name, value in (headers or {}).items():
            req.add_header(name, value)
        try:
            with urllib.request.urlopen(req, timeout=10) as response:
                return response.status, json.loads(response.read()), dict(response.headers)
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read()), dict(error.headers)


class HttpTest(ServerCase):
    def test_probes_match_the_pos_shape(self) -> None:
        self.assertEqual(
            self.request("GET", "/health")[:2], (200, {"status": "ok", "service": "payments"})
        )
        self.assertEqual(
            self.request("GET", "/ready")[:2], (200, {"status": "ready", "service": "payments"})
        )
        status, body, _ = self.request("GET", "/version")
        self.assertEqual(
            (status, body),
            (200, {"service": "payments", "commit": "abc123", "image_digest": "unknown"}),
        )

    def test_a_payment_round_trip_over_http(self) -> None:
        status, created, _ = self.request(
            "POST",
            "/payments",
            {
                "tenant_id": "t1",
                "msisdn": "254000000001",
                "amount": 150000,
                "account_reference": "r1",
            },
            {"Idempotency-Key": KEY},
        )
        self.assertEqual((status, created["state"]), (201, "PENDING"))
        self.request("POST", "/_fake/advance", {"seconds": 2})
        status, delivered, _ = self.request("POST", "/_fake/deliver-callbacks", {})
        self.assertEqual(status, 200)
        self.assertEqual(delivered["delivered"][0]["status"], "applied")
        status, view, _ = self.request("GET", "/payments/" + created["payment_id"])
        self.assertEqual((status, view["state"], view["ledger_entries"]), (200, "SUCCEEDED", 1))
        again = self.request(
            "POST",
            "/payments",
            {
                "tenant_id": "t1",
                "msisdn": "254000000001",
                "amount": 150000,
                "account_reference": "r1",
            },
            {"Idempotency-Key": KEY},
        )
        self.assertEqual(again[0], 200)
        self.assertEqual(again[2].get("Idempotent-Replayed"), "true")

    def test_a_payout_round_trip_over_http(self) -> None:
        body = {
            "tenant_id": "t1",
            "attendant_id": "a1",
            "payout_period": "2026-09-20",
            "msisdn": "254000000101",
            "amount": 500000,
        }
        status, created, _ = self.request("POST", "/payouts", body, {"Idempotency-Key": KEY})
        self.assertEqual((status, created["state"]), (201, "PENDING"))
        self.request("POST", "/_fake/advance", {"seconds": 2})
        self.request("POST", "/_fake/deliver-callbacks", {})
        status, view, _ = self.request("GET", "/payouts/" + created["disbursement_id"])
        self.assertEqual((status, view["state"], view["ledger_entries"]), (200, "SUCCEEDED", 1))

    def test_errors(self) -> None:
        self.assertEqual(self.request("GET", "/nope")[0], 404)
        self.assertEqual(self.request("POST", "/nope", {})[0], 404)
        self.assertEqual(
            self.request("POST", "/payments", b"{bad", {"Idempotency-Key": KEY})[0], 400
        )
        self.assertEqual(self.request("POST", "/payments", {"a": 1})[0], 400)
        self.assertEqual(self.request("POST", "/_fake/advance", {"seconds": -1})[0], 400)
        big = b"x" * (64 * 1024 + 1)
        self.assertEqual(self.request("POST", "/payments", big, {"Idempotency-Key": KEY})[0], 413)

    def test_the_callback_route_answers_200_for_a_valid_replayed_callback(self) -> None:
        _, created, _ = self.request(
            "POST",
            "/payments",
            {
                "tenant_id": "t1",
                "msisdn": "254000000009",
                "amount": 100000,
                "account_reference": "r",
            },
            {"Idempotency-Key": KEY},
        )
        self.request("POST", "/_fake/advance", {"seconds": 2})  # so the status query confirms
        delivery = self.app.adapter.scheduled_callbacks(created["checkout_request_id"])[0]
        for _ in range(3):
            status, body, _ = self.request(
                "POST", "/payments/daraja/callback", delivery.body, delivery.headers
            )
            self.assertEqual((status, body["ResultCode"]), (200, 0))
        self.assertEqual(
            self.app.payments.get_payment(created["payment_id"]).body["ledger_entries"], 1
        )


class BlockedSourceTest(ServerCase):
    allowed_ips = ("10.9.9.9",)

    def test_a_source_outside_the_allowlist_gets_403(self) -> None:
        _, created, _ = self.request(
            "POST",
            "/payments",
            {
                "tenant_id": "t1",
                "msisdn": "254000000001",
                "amount": 100000,
                "account_reference": "r",
            },
            {"Idempotency-Key": KEY},
        )
        delivery = self.app.adapter.scheduled_callbacks(created["checkout_request_id"])[0]
        status, body, _ = self.request(
            "POST", "/payments/daraja/callback", delivery.body, delivery.headers
        )
        self.assertEqual((status, body["error"]), (403, "source_not_allowed"))


class StartupRefusalTest(unittest.TestCase):
    """Run the real entry point: it must refuse, loudly, and never fall back to the fake."""

    def run_app(self, extra: dict[str, str]) -> subprocess.CompletedProcess:
        env = {k: v for k, v in os.environ.items() if not k.startswith(("MPESA_", "DATABASE_"))}
        env.update(extra)
        return subprocess.run(
            [sys.executable, str(SERVICE_DIR / "app.py")],
            env=env,
            cwd=SERVICE_DIR,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

    def test_daraja_sandbox_without_credentials_refuses_to_start(self) -> None:
        result = self.run_app({"MPESA_ADAPTER": "daraja_sandbox"})
        self.assertEqual(result.returncode, 2)
        self.assertIn("refusing to start", result.stderr)
        self.assertNotIn("listening", result.stdout)

    def test_daraja_sandbox_with_every_credential_still_refuses(self) -> None:
        extra = {
            "MPESA_ADAPTER": "daraja_sandbox",
            "MPESA_CONSUMER_KEY": "x",
            "MPESA_CONSUMER_SECRET": "x",
            "MPESA_SHORTCODE": "x",
            "MPESA_PASSKEY": "x",
            "MPESA_CALLBACK_BASE_URL": "https://example.invalid",
        }
        result = self.run_app(extra)
        self.assertEqual(result.returncode, 2)
        self.assertIn("only 'fake' is available", result.stderr)

    def test_postgres_url_refuses_to_start(self) -> None:
        result = self.run_app({"DATABASE_URL": "postgres://u@h/db"})
        self.assertEqual(result.returncode, 2)


if __name__ == "__main__":
    unittest.main()
