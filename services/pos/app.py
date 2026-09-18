#!/usr/bin/env python3
"""Minimal POS stub for G1 ECS golden path."""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("PORT", "8080"))
COMMIT_SHA = os.environ.get("COMMIT_SHA", "local")
IMAGE_DIGEST = os.environ.get("IMAGE_DIGEST", "unknown")
ADOT_HEALTH_URL = os.environ.get("ADOT_HEALTH_URL", "http://127.0.0.1:13133/")


def adot_healthy() -> bool:
    try:
        with urllib.request.urlopen(ADOT_HEALTH_URL, timeout=2) as resp:
            return 200 <= resp.status < 300
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _json(self, code: int, body: dict) -> None:
        payload = json.dumps(body).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/health":
            self._json(200, {"status": "ok", "service": "pos"})
            return
        if self.path == "/ready":
            if not adot_healthy():
                self._json(503, {"status": "not_ready", "service": "pos", "reason": "adot_unhealthy"})
                return
            self._json(200, {"status": "ready", "service": "pos"})
            return
        if self.path == "/version":
            self._json(
                200,
                {
                    "service": "pos",
                    "commit": COMMIT_SHA,
                    "image_digest": IMAGE_DIGEST,
                },
            )
            return
        self._json(404, {"error": "not_found"})


def main() -> None:
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"pos stub listening on {PORT}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
