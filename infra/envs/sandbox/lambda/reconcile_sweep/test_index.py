"""Unit tests for the scheduled reconcile sweep. No AWS; a local HTTP server stands in for Payments.

    cd infra/envs/sandbox/lambda/reconcile_sweep && python3 -m unittest -v
"""

from __future__ import annotations

import json
import os
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import index


def serve(status: int, body: bytes):
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            seen.append((self.command, self.path))
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}/_admin/sweep", seen


class SweepTest(unittest.TestCase):
    def test_successful_pass_posts_to_sweep(self):
        server, url, seen = serve(200, json.dumps({"payments": {}, "payouts": {}}).encode())
        self.addCleanup(server.shutdown)
        os.environ["SWEEP_URL"] = url
        self.assertTrue(index.handler({}, None)["ok"])
        self.assertEqual(seen, [("POST", "/_admin/sweep")])

    def test_failed_pass_raises_so_the_invocation_is_an_error(self):
        for status in (500, 503, 404):
            server, url, _ = serve(status, b'{"error":"x"}')
            self.addCleanup(server.shutdown)
            os.environ["SWEEP_URL"] = url
            with self.assertRaises(index.SweepFailed, msg=status):
                index.handler({}, None)

    def test_unreachable_payments_raises(self):
        os.environ["SWEEP_URL"] = "http://127.0.0.1:9/_admin/sweep"
        with self.assertRaises(index.SweepFailed):
            index.handler({}, None)


if __name__ == "__main__":
    unittest.main()
