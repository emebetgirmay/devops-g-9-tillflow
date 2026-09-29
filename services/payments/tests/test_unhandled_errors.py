"""An unexpected exception becomes a counted, logged 500, never a dropped connection.

Before this, an exception escaping a route killed the handler thread with no reply; behind the
ALB that was a 502 that reached none of Payments' own signals (the G3 k6 soak's 110 silent 502s,
evidence/reliability-ops/k6-analysis.md). These tests force one and check every signal sees it.
"""

from __future__ import annotations

import contextlib
import io
import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import _bootstrap  # noqa: F401
from app import make_handler
from helpers import ServiceTestCase


class UnhandledErrorTest(ServiceTestCase):
    def break_route(self) -> None:
        def boom(_path):
            raise RuntimeError("dictionary changed size during iteration")

        self.app._get = boom  # an instance attribute shadows the method for this test only

    def test_dispatch_answers_500_with_the_trace_id(self) -> None:
        self.break_route()
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            reply = self.call("GET", "/payments/pay_x")
        self.assertEqual(reply.status, 500)
        self.assertEqual(reply.body["error"], "internal_error")
        self.assertEqual(reply.body["trace_id"], reply.headers["X-Trace-Id"])

        lines = [json.loads(line) for line in out.getvalue().splitlines() if line.startswith("{")]
        events = {line["event"] for line in lines}
        self.assertIn("unhandled_error", events)
        error = next(line for line in lines if line["event"] == "unhandled_error")
        self.assertEqual(error["level"], "ERROR")
        self.assertIn("RuntimeError", error["result"])
        self.assertIn("Traceback", err.getvalue())

    def test_the_5xx_is_counted_so_burn_alarms_can_see_it(self) -> None:
        self.break_route()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.call("GET", "/payments/pay_x")
        del self.app._get  # back to the real routes, so GET /metrics itself works
        metrics = self.call("GET", "/metrics").body.decode()
        self.assertIn('payments_http_requests_total{route="/payments/{id}",status_class="5xx"} 1', metrics)

    def test_over_real_http_the_client_gets_a_500_not_a_dropped_connection(self) -> None:
        self.break_route()
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.app))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        url = f"http://127.0.0.1:{server.server_address[1]}/payments/pay_x"
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(url, timeout=5)
        self.assertEqual(ctx.exception.code, 500)
        self.assertEqual(json.loads(ctx.exception.read())["error"], "internal_error")


if __name__ == "__main__":
    unittest.main()
