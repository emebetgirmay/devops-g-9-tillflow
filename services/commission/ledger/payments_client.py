"""Outbound client Commission uses to talk to the Payments API — POST /payouts and its GET, only.

stdlib only (``urllib``). This is Commission's *only* path to moving money (ADR 0004, ADR 0008):
it never imports the M-Pesa adapter and never talks to Daraja directly.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request


class PaymentsError(Exception):
    """Payments could not be reached, or answered with something unusable."""


def create_payout(
    base_url: str, idempotency_key: str, body: dict, timeout: float = 30.0
) -> tuple[int, dict]:
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/payouts",
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json", "Idempotency-Key": idempotency_key},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        try:
            return error.code, json.loads(error.read())
        except ValueError:
            return error.code, {}
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise PaymentsError(f"POST /payouts unreachable: {exc}") from exc


def get_payout(base_url: str, disbursement_id: str, timeout: float = 30.0) -> dict:
    """Passive read of Payments' last-known state. Does not ask Payments to check with the
    adapter -- use ``reconcile_payout`` for that. Kept for callers that only want a status
    display and should not trigger work."""
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/payouts/{disbursement_id}", method="GET"
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as error:
        if error.code == 404:
            raise PaymentsError(f"payout {disbursement_id} not found") from error
        body = error.read().decode(errors="replace")
        raise PaymentsError(
            f"GET /payouts/{disbursement_id} returned {error.code}: {body[:300]}"
        ) from error
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        raise PaymentsError(
            f"GET /payouts/{disbursement_id} unreachable or unparseable: {exc}"
        ) from exc


def reconcile_payout(base_url: str, disbursement_id: str, timeout: float = 30.0) -> dict:
    """Ask Payments to check this disbursement against the adapter now, if it is eligible
    (Payments only actually queries when its own state is UNKNOWN or NEEDS_REVIEW; a still-PENDING
    disbursement has not crossed Payments' own callback deadline yet, and this call does not force
    that -- Payments' scheduled sweep is what promotes PENDING to UNKNOWN). The response always
    includes the current ``state``, whether or not anything changed."""
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/payouts/{disbursement_id}/reconcile", data=b"", method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as error:
        if error.code == 404:
            raise PaymentsError(f"payout {disbursement_id} not found") from error
        body = error.read().decode(errors="replace")
        raise PaymentsError(
            f"POST /payouts/{disbursement_id}/reconcile returned {error.code}: {body[:300]}"
        ) from error
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        raise PaymentsError(
            f"POST /payouts/{disbursement_id}/reconcile unreachable or unparseable: {exc}"
        ) from exc
