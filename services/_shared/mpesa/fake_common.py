"""Helpers shared by the fake collection and disbursement adapters (no network, no randomness)."""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from typing import Protocol

SIGNATURE_HEADER = "X-Fake-Signature"
DEFAULT_SIGNING_KEY = b"fake-adapter-test-key"


class Clock(Protocol):
    def now(self) -> float: ...


class ManualClock:
    """Test clock. Time only moves when advance() is called."""

    def __init__(self, start: float = 0.0) -> None:
        self._now = start

    def now(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError("cannot move the clock backwards")
        self._now += seconds


@dataclass(frozen=True)
class FakeAdapterConfig:
    callback_delay_s: float = 1.0
    late_after_s: float = 120.0
    duplicate_count: int = 3
    signing_key: bytes = DEFAULT_SIGNING_KEY


@dataclass(frozen=True)
class CallbackDelivery:
    """One callback the provider would POST to the callback handler. For a disbursement result,
    provider_ref holds the originator conversation id."""

    provider_ref: str
    deliver_at: float
    headers: dict[str, str]
    body: bytes


def sign(body: bytes, key: bytes = DEFAULT_SIGNING_KEY) -> str:
    return hmac.new(key, body, hashlib.sha256).hexdigest()
