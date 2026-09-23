"""Outbound client Commission uses to read confirmed-paid sales from POS.

stdlib only (``urllib``), matching worker.py's existing Payments client style. Read-only: this
client has no method that could write to POS. Paginates via POS's keyset cursor
(``services/pos/app/routers/commission.py``) until a page comes back with no ``next_cursor``.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass


class POSError(Exception):
    """POS could not be reached, or answered with something unusable."""


@dataclass(frozen=True)
class PaidSale:
    sale_id: str
    attendant_id: str
    attendant_msisdn: str
    commission_rate_bps: int
    total_minor: int
    currency: str
    paid_at: str


class POSClient:
    def __init__(self, base_url: str, timeout: float = 30.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _get(self, path: str, params: dict[str, str]) -> dict:
        url = f"{self.base_url}{path}?{urllib.parse.urlencode(params)}"
        request = urllib.request.Request(url, method="GET")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as error:
            body = error.read().decode(errors="replace")
            raise POSError(f"POS {path} returned {error.code}: {body[:300]}") from error
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as error:
            raise POSError(f"POS {path} unreachable or unparseable: {error}") from error

    def paid_sales(
        self, tenant_id: str, since: str, until: str, *, limit: int = 500
    ) -> Iterator[PaidSale]:
        """Yield every PAID sale in [since, until) for a tenant, walking every page.

        `since`/`until` are ISO 8601 strings (the same format POS accepts and returns).
        """
        cursor: str | None = None
        while True:
            params = {"since": since, "until": until, "limit": str(limit)}
            if cursor:
                params["cursor"] = cursor
            page = self._get(f"/tenants/{tenant_id}/commission/paid-sales", params)
            try:
                sales = page["sales"]
            except (KeyError, TypeError) as exc:
                raise POSError(f"malformed paid-sales response: {page!r}") from exc
            for raw in sales:
                yield PaidSale(
                    sale_id=raw["sale_id"],
                    attendant_id=raw["attendant_id"],
                    attendant_msisdn=raw["attendant_msisdn"],
                    commission_rate_bps=raw["commission_rate_bps"],
                    total_minor=raw["total_minor"],
                    currency=raw["currency"],
                    paid_at=raw["paid_at"],
                )
            cursor = page.get("next_cursor")
            if not cursor:
                return
