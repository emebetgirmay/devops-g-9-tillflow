"""Operator and test paths are refused from the public edge on a real-adapter build (ADR 0009 G3-7).

API Gateway stamps every request it forwards with x-tillflow-edge. With the FakeAdapter (the
sandbox) the drills and demos that call /_fake and /_admin through the public URL keep working;
with a real adapter those paths answer 404 from the internet, while in-VPC callers (the
scheduled sweep, k6) still reach /_admin/sweep.
"""

from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

import _bootstrap  # noqa: F401
from app import App
from core.config import Settings
from mpesa import ManualClock

EDGE = {"x-tillflow-edge": "public"}  # what API Gateway sets (infra/envs/sandbox/api_gateway.tf)


class RealAdapterStandIn:
    """Anything that is not the FakeAdapter counts as a real provider for this rule."""


def make_app(adapter=None) -> App:
    tmp = tempfile.TemporaryDirectory()
    settings = replace(Settings(), db_path=str(Path(tmp.name) / "payments.db"))
    app = App(settings, clock=ManualClock(1_000_000.0), adapter=adapter)
    app._tmp = tmp  # keep the directory alive as long as the app
    return app


def call(app: App, method: str, path: str, headers=None):
    return app.dispatch(method, path, headers or {}, b"{}" if method == "POST" else b"", "10.9.0.10")


class PublicEdgeTest(unittest.TestCase):
    def test_real_adapter_refuses_operator_and_test_paths_from_the_edge(self) -> None:
        app = make_app(RealAdapterStandIn())
        for method, path in (
            ("POST", "/_admin/sweep"),
            ("GET", "/_admin/invariants"),
            ("POST", "/_fake/advance"),
            ("POST", "/_fake/deliver-callbacks"),
        ):
            reply = call(app, method, path, EDGE)
            self.assertEqual((reply.status, reply.body), (404, {"error": "not_found"}), path)

    def test_real_adapter_still_serves_the_sweep_inside_the_vpc(self) -> None:
        app = make_app(RealAdapterStandIn())
        reply = call(app, "POST", "/_admin/sweep")  # no edge header: the scheduled Lambda, k6
        self.assertEqual(reply.status, 200)
        self.assertIn("payments", reply.body)

    def test_fake_adapter_keeps_the_sandbox_drills_working_through_the_edge(self) -> None:
        app = make_app()  # FakeAdapter
        self.assertEqual(call(app, "POST", "/_admin/sweep", EDGE).status, 200)
        self.assertEqual(call(app, "GET", "/_admin/invariants", EDGE).status, 200)
        self.assertEqual(call(app, "POST", "/_fake/deliver-callbacks", EDGE).status, 200)

    def test_ordinary_paths_are_never_affected(self) -> None:
        app = make_app(RealAdapterStandIn())
        self.assertEqual(call(app, "GET", "/health", EDGE).status, 200)
        self.assertEqual(call(app, "GET", "/payments/pay_missing", EDGE).status, 404)  # the service's own 404


if __name__ == "__main__":
    unittest.main()
