"""A minimal, deterministic stand-in for POS's paid-sales endpoint, for Commission's own tests.

Implements exactly the contract in services/pos/app/routers/commission.py (same query params,
same keyset cursor shape, same response body) over a real stdlib HTTP server, so
ledger/pos_client.py is exercised as a genuine HTTP round trip rather than mocked in-process. It is
not a reimplementation of POS's business logic -- sales are seeded directly as already-PAID rows;
whether a sale legitimately reached PAID is POS's own job and POS's own tests
(services/pos/tests/test_commission_paid_sales.py) cover that side of the contract.
"""

from __future__ import annotations

import base64
import json
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse


@dataclass
class FakePOSSale:
    sale_id: str
    attendant_id: str
    attendant_msisdn: str
    commission_rate_bps: int
    total_minor: int
    currency: str
    paid_at: str  # ISO 8601


@dataclass
class FakePOSServer:
    sales: dict[str, list[FakePOSSale]] = field(default_factory=dict)  # tenant_id -> sales

    def __post_init__(self) -> None:
        sales = self.sales
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt: str, *args) -> None:
                pass

            def do_GET(self) -> None:
                parsed = urlparse(self.path)
                parts = parsed.path.strip("/").split("/")
                if (
                    len(parts) != 4
                    or parts[0] != "tenants"
                    or parts[2:] != ["commission", "paid-sales"]
                ):
                    self._send(404, {"detail": "not found"})
                    return
                tenant_id = parts[1]
                qs = parse_qs(parsed.query)
                since = qs.get("since", [None])[0]
                until = qs.get("until", [None])[0]
                if not since or not until:
                    self._send(400, {"detail": "since and until are required"})
                    return
                attendant_id = qs.get("attendant_id", [None])[0]
                limit = int(qs.get("limit", ["500"])[0])
                cursor = qs.get("cursor", [None])[0]

                rows = sorted(sales.get(tenant_id, []), key=lambda s: (s.paid_at, s.sale_id))
                rows = [s for s in rows if since <= s.paid_at < until]
                if attendant_id:
                    rows = [s for s in rows if s.attendant_id == attendant_id]
                if cursor:
                    cursor_paid_at, cursor_id = outer._decode_cursor(cursor)
                    rows = [s for s in rows if (s.paid_at, s.sale_id) > (cursor_paid_at, cursor_id)]

                page = rows[:limit]
                next_cursor = None
                if len(rows) > limit:
                    next_cursor = outer._encode_cursor(page[-1].paid_at, page[-1].sale_id)

                self._send(
                    200,
                    {
                        "sales": [
                            {
                                "sale_id": s.sale_id,
                                "attendant_id": s.attendant_id,
                                "attendant_msisdn": s.attendant_msisdn,
                                "commission_rate_bps": s.commission_rate_bps,
                                "total_minor": s.total_minor,
                                "currency": s.currency,
                                "paid_at": s.paid_at,
                            }
                            for s in page
                        ],
                        "next_cursor": next_cursor,
                    },
                )

            def _send(self, status: int, body: dict) -> None:
                payload = json.dumps(body).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @staticmethod
    def _encode_cursor(paid_at: str, sale_id: str) -> str:
        return base64.urlsafe_b64encode(f"{paid_at}|{sale_id}".encode()).decode("ascii")

    @staticmethod
    def _decode_cursor(cursor: str) -> tuple[str, str]:
        paid_at, sale_id = base64.urlsafe_b64decode(cursor.encode("ascii")).decode().split("|", 1)
        return paid_at, sale_id

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def add(self, tenant_id: str, sale: FakePOSSale) -> None:
        self.sales.setdefault(tenant_id, []).append(sale)

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
