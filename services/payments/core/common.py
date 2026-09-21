"""Shared small pieces: the reply type, backoff schedule, ids and request parsing helpers."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any

# ADR 0006 section 3: query at +30 s, +1 m, +2 m, +5 m, +10 m, +30 m, +1 h, then hourly. The
# ADR mentions jitter; it is omitted here so runs stay deterministic.
BACKOFF_SECONDS = (30, 60, 120, 300, 600, 1800, 3600)

PROVIDER = "mpesa"
TEXT_RE = re.compile(r"^[A-Za-z0-9_.:@-]{1,64}$")


@dataclass
class Reply:
    """An HTTP-level answer, kept independent of the server so it is easy to test."""

    status: int
    body: dict[str, Any]
    headers: dict[str, str] = field(default_factory=dict)


def next_backoff(attempts: int) -> int:
    return BACKOFF_SECONDS[min(attempts, len(BACKOFF_SECONDS) - 1)]


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def fingerprint(fields: dict[str, Any]) -> str:
    """SHA-256 of a canonical form of the semantic request fields (ADR 0006 section 2)."""
    return sha256_hex(json.dumps(fields, sort_keys=True, separators=(",", ":")))


def adapter_key(tenant_id: str, operation: str, key: str) -> str:
    """The idempotency key passed to the adapter: unique across tenants and operations."""
    return sha256_hex(f"{tenant_id}|{operation}|{key}")[:40]


def mask_msisdn(msisdn: str) -> str:
    return f"{msisdn[:4]}****{msisdn[-3:]}"


def is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)
