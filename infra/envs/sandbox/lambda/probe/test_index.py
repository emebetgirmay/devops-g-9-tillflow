"""Unit tests for the edge probe. No AWS; the HTTP check runs against a local server.

    cd infra/envs/sandbox/lambda/probe && python3 -m unittest -v
"""

from __future__ import annotations

import json
import os
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import index


class FakeCloudWatch:
    def __init__(self):
        self.calls = []

    def put_metric_data(self, **kwargs):
        self.calls.append(kwargs)


def serve(status: int, body: bytes):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}/ready"


class ProbeTest(unittest.TestCase):
    def run_probe(self, status, body):
        server, url = serve(status, body)
        self.addCleanup(server.shutdown)
        os.environ["PROBE_URL"] = url
        cw = FakeCloudWatch()
        result = index.handler({}, None, client=cw)
        values = {m["MetricName"]: m["Value"] for m in cw.calls[0]["MetricData"]}
        self.assertEqual(cw.calls[0]["Namespace"], "TillFlow/Probe")
        return result, values

    def test_ready_service_writes_success(self):
        result, values = self.run_probe(200, json.dumps({"status": "ready"}).encode())
        self.assertTrue(result["ok"])
        self.assertEqual(values["ProbeSuccess"], 1.0)
        self.assertGreaterEqual(values["ProbeLatencyMs"], 0)

    def test_not_ready_or_error_writes_failure_instead_of_raising(self):
        for status, body in ((503, b'{"status":"not_ready"}'), (200, b'{"status":"starting"}'), (200, b"<html>")):
            result, values = self.run_probe(status, body)
            self.assertFalse(result["ok"], (status, body))
            self.assertEqual(values["ProbeSuccess"], 0.0)

    def test_unreachable_edge_writes_failure(self):
        os.environ["PROBE_URL"] = "http://127.0.0.1:9/ready"
        cw = FakeCloudWatch()
        result = index.handler({}, None, client=cw)
        self.assertFalse(result["ok"])
        self.assertEqual(cw.calls[0]["MetricData"][0]["Value"], 0.0)


if __name__ == "__main__":
    unittest.main()
