"""Small pieces shared across worker.py (the CSV entrypoint) and ledger/ (the ledger-driven path)."""

from __future__ import annotations

import hashlib


def idempotency_key_for(tenant_id: str, attendant_id: str, payout_period: str) -> str:
    """Deterministic, 43 characters of [A-Za-z0-9_]: the same payout always gets the same key.

    Same three-field input (tenant, attendant, period/business_date) that Payments' own
    ``derive_payout_key`` hashes (services/payments/core/payouts.py), so the two stay aligned
    without Commission importing Payments' code.
    """
    digest = hashlib.sha256(f"{tenant_id}|{attendant_id}|{payout_period}".encode()).hexdigest()
    return "po_" + digest[:40]


def classify_payout_response(status: int, reply: dict) -> str:
    """Map a Payments POST /payouts response to an outcome label. Shared by worker.py (the CSV
    path) and ledger/disburse.py (the ledger path) so both interpret Payments identically."""
    error = reply.get("error", "")
    if status == 201:
        return "created"
    if status == 200:
        return "already_requested"
    if status == 409 and error == "payout_already_requested":
        return "already_requested"
    if status == 409 and error == "idempotency_in_flight":
        return "in_flight_retry_next_run"
    if status == 409:
        return "conflict_needs_review"
    if status == 422:
        return f"held_{error}"
    if status == 503 and error == "payouts_disabled":
        return "payouts_disabled"
    return f"rejected_{status}"
