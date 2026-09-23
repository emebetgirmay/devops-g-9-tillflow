"""The real pipeline, end to end: POS (real FastAPI app) -> Commission's close -> Commission's
disburse -> Payments (real app, FakeAdapter) -> Payments' own ledger.

This is the strongest proof available without a deployed stack: three real service codebases in
one process, talking over real HTTP, none of them mocked. Everything else in this test suite
covers the same properties in isolation (test_close.py against a contract-level FakePOSServer,
test_disburse.py against a real in-process Payments); this module exists to prove the seams
between the real services agree with each other too -- the same spirit as
services/_shared/pos-payments-contract.md's "Verified live" section for POS <-> Payments.

Skipped automatically if POS's dependencies (fastapi, sqlalchemy, uvicorn) are not installed --
this suite otherwise stays stdlib-only. Install with:
    pip install -r ../pos/requirements.txt
"""

from __future__ import annotations

import socket
import tempfile
import threading
import time
import unittest
import uuid
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import _bootstrap  # noqa: F401

try:
    import uvicorn
    from fastapi.testclient import TestClient  # noqa: F401  (import-checked, unused directly)

    _HAVE_POS_DEPS = True
except ImportError:
    _HAVE_POS_DEPS = False

from app import App as PaymentsApp
from app import make_handler as make_payments_handler
from core.config import Settings as PaymentsSettings
from mpesa import FakeAdapter, ManualClock

from ledger.close import close_business_day
from ledger.config import Settings as CommissionSettings
from ledger.disburse import reconcile_requested_payouts, send_due_payouts
from ledger.pos_client import POSClient
from ledger.store import Store as CommissionStore

if _HAVE_POS_DEPS:
    import os
    import sys
    import urllib.request
    from http.server import ThreadingHTTPServer

    POS_DIR = Path(__file__).resolve().parent.parent.parent / "pos"

    def _free_port() -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]

    def _run_pos_app(db_path: str, port: int) -> uvicorn.Server:
        """Import and serve a fresh copy of POS's FastAPI app, bound to `db_path`.

        POS reads DATABASE_URL once at import time (app/db.py module scope), so the env var must
        be set before the first import in this process; a later test re-importing an
        already-cached `app.db` would silently keep the first test's database, so each test gets
        its own fully isolated subinterpreter-style import via `runpy`-free module purge instead.
        """
        if str(POS_DIR) not in sys.path:
            sys.path.insert(0, str(POS_DIR))
        for name in list(sys.modules):
            if name == "app" or name.startswith("app."):
                del sys.modules[name]
        os.environ["DATABASE_URL"] = f"sqlite:///{db_path}"
        from app.main import app as pos_app

        config = uvicorn.Config(pos_app, host="127.0.0.1", port=port, log_level="warning")
        server = uvicorn.Server(config)
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        deadline = time.time() + 10
        while not server.started and time.time() < deadline:
            time.sleep(0.02)
        if not server.started:
            raise RuntimeError("POS app did not start in time")
        server._e2e_thread = thread  # for teardown
        return server

    def _stop_pos_app(server: uvicorn.Server) -> None:
        server.should_exit = True
        server._e2e_thread.join(timeout=10)


@unittest.skipUnless(_HAVE_POS_DEPS, "POS deps (fastapi/sqlalchemy/uvicorn) not installed")
class EndToEndTest(unittest.TestCase):
    def setUp(self) -> None:
        self._dirs = [tempfile.TemporaryDirectory() for _ in range(3)]
        for d in self._dirs:
            self.addCleanup(d.cleanup)
        pos_dir, payments_dir, commission_dir = self._dirs

        self.clock = ManualClock(1_000_000.0)
        self.adapter = FakeAdapter(clock=self.clock)
        payments_settings = replace(
            PaymentsSettings(), db_path=str(Path(payments_dir.name) / "p.db")
        )
        self.payments_app = PaymentsApp(payments_settings, clock=self.clock, adapter=self.adapter)
        self.payments_server = ThreadingHTTPServer(
            ("127.0.0.1", 0), make_payments_handler(self.payments_app)
        )
        threading.Thread(target=self.payments_server.serve_forever, daemon=True).start()
        self.addCleanup(self.payments_server.server_close)
        self.addCleanup(self.payments_server.shutdown)
        self.payments_url = f"http://127.0.0.1:{self.payments_server.server_address[1]}"

        os.environ["PAYMENTS_BASE_URL"] = self.payments_url
        pos_port = _free_port()
        self.pos_server = _run_pos_app(str(Path(pos_dir.name) / "pos.db"), pos_port)
        self.addCleanup(_stop_pos_app, self.pos_server)
        self.pos_url = f"http://127.0.0.1:{pos_port}"

        self.commission_settings = replace(
            CommissionSettings(),
            db_path=str(Path(commission_dir.name) / "c.db"),
            pos_base_url=self.pos_url,
            payments_base_url=self.payments_url,
        )
        self.commission_store = CommissionStore(self.commission_settings.db_path)
        self.pos_client = POSClient(self.pos_url)

    # --- POS setup helpers, over real HTTP (POS's own API, nothing bypassed) -----------------

    def _pos_post(self, path: str, body: dict, headers: dict | None = None) -> dict:
        data = __import__("json").dumps(body).encode()
        request = urllib.request.Request(
            f"{self.pos_url}{path}",
            data=data,
            method="POST",
            headers={"Content-Type": "application/json", **(headers or {})},
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            return __import__("json").loads(response.read())

    def _make_paid_sale(
        self, tenant_id: str, till_id: str, attendant_id: str, product_id: str
    ) -> dict:
        sale = self._pos_post(
            f"/tenants/{tenant_id}/sales",
            {
                "till_id": till_id,
                "attendant_id": attendant_id,
                "line_items": [{"product_id": product_id, "quantity": 2}],
            },
            {"Idempotency-Key": str(uuid.uuid4())},
        )
        requested = self._pos_post(
            f"/tenants/{tenant_id}/sales/{sale['id']}/payment-request", {"phone": "254712345678"}
        )
        payment_id = requested["payment_id"]
        event = self._pos_post(
            f"/internal/sales/{sale['id']}/payment-events",
            {
                "event_id": f"evt-{uuid.uuid4()}",
                "payment_id": payment_id,
                "status": "PAID",
                "amount_minor": sale["total_minor"],
            },
        )
        assert event["status"] == "PAID", event
        return sale

    def test_the_whole_pipeline_pays_an_attendant_exactly_once(self) -> None:
        tenant = self._pos_post("/tenants", {"name": "Acme Duka"})
        till = self._pos_post(f"/tenants/{tenant['id']}/tills", {"name": "Till 1"})
        attendant = self._pos_post(
            f"/tenants/{tenant['id']}/attendants",
            {"name": "Mary", "phone": "254000000101", "commission_rate_bps": 500},
        )
        product = self._pos_post(
            f"/tenants/{tenant['id']}/products",
            {"sku": "SODA-500", "name": "Soda 500ml", "price_minor": 8000, "currency": "KES"},
        )

        for _ in range(3):
            self._make_paid_sale(tenant["id"], till["id"], attendant["id"], product["id"])
        # Total: 3 sales x 2 units x 80 KES = 480 KES; 5% commission = 24.00 KES = 2,400 minor.

        today = datetime.now(timezone.utc).date()
        close_result = close_business_day(
            self.commission_store, self.pos_client, self.commission_settings, tenant["id"], today
        )
        self.assertEqual((close_result["sales_seen"], close_result["attendants_closed"]), (3, 1))

        with self.commission_store.connection() as conn:
            (ledger,) = conn.execute("SELECT * FROM payout_ledger").fetchall()
        self.assertEqual((ledger["amount_minor"], ledger["state"]), (2_400, "PLANNED"))

        send_due_payouts(self.commission_store, self.commission_settings)
        self.clock.advance(2)
        self.payments_app.dispatch("POST", "/_fake/deliver-callbacks", {}, b"{}", "127.0.0.1")
        reconcile_requested_payouts(self.commission_store, self.commission_settings)

        with self.commission_store.connection() as conn:
            ledger = conn.execute(
                "SELECT * FROM payout_ledger WHERE id = ?", (ledger["id"],)
            ).fetchone()
        self.assertEqual(ledger["state"], "SUCCEEDED")

        with self.payments_app.store.connection() as conn:
            entries = conn.execute(
                "SELECT * FROM ledger_entries WHERE entry_type = 'DISBURSEMENT_DEBIT'"
            ).fetchall()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["amount_minor"], 2_400)

        # Running the whole pipeline again -- close, send, reconcile -- changes nothing: no
        # second ledger row, no second disbursement call, no second debit.
        close_business_day(
            self.commission_store, self.pos_client, self.commission_settings, tenant["id"], today
        )
        send_due_payouts(self.commission_store, self.commission_settings)
        reconcile_requested_payouts(self.commission_store, self.commission_settings)
        with self.commission_store.connection() as conn:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) AS n FROM payout_ledger").fetchone()["n"], 1
            )
        with self.payments_app.store.connection() as conn:
            n = conn.execute(
                "SELECT COUNT(*) AS n FROM ledger_entries WHERE entry_type = 'DISBURSEMENT_DEBIT'"
            ).fetchone()["n"]
        self.assertEqual(n, 1)
        self.assertEqual(self.adapter.disburse_call_count, 1)

    def test_carry_forward_and_a_second_attendant_stay_independent_through_the_real_pipeline(
        self,
    ) -> None:
        tenant = self._pos_post("/tenants", {"name": "Acme Duka"})
        till = self._pos_post(f"/tenants/{tenant['id']}/tills", {"name": "Till 1"})
        low_rate = self._pos_post(
            f"/tenants/{tenant['id']}/attendants",
            {"name": "Low", "phone": "254000000101", "commission_rate_bps": 10},
        )
        high_rate = self._pos_post(
            f"/tenants/{tenant['id']}/attendants",
            {"name": "High", "phone": "254000000102", "commission_rate_bps": 5000},
        )
        product = self._pos_post(
            f"/tenants/{tenant['id']}/products",
            {"sku": "GUM-1", "name": "Gum", "price_minor": 2_000, "currency": "KES"},
        )
        self._make_paid_sale(
            tenant["id"], till["id"], low_rate["id"], product["id"]
        )  # 0.1% of 40 KES = 4 minor: carried
        self._make_paid_sale(
            tenant["id"], till["id"], high_rate["id"], product["id"]
        )  # 50% of 40 KES = 2000 minor: payable

        today = datetime.now(timezone.utc).date()
        close_business_day(
            self.commission_store, self.pos_client, self.commission_settings, tenant["id"], today
        )

        with self.commission_store.connection() as conn:
            rows = conn.execute("SELECT attendant_id, amount_minor FROM payout_ledger").fetchall()
            carry = conn.execute(
                "SELECT amount_minor FROM carry_forward WHERE attendant_id = ?", (low_rate["id"],)
            ).fetchone()
        self.assertEqual(
            {r["attendant_id"]: r["amount_minor"] for r in rows}, {high_rate["id"]: 2_000}
        )
        self.assertGreater(carry["amount_minor"], 0)  # low_rate's sub-minimum commission, not lost


if __name__ == "__main__":
    unittest.main()
