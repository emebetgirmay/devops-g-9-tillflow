"""Deterministic FakeAdapter for CI, tests and k6 (ADR 0004, ADR 0006).

No network, no real credentials, no unseeded randomness. Time comes from an injected clock.
Scenarios are selected by magic MSISDNs (see MAGIC_MSISDNS and the README). The callback
payload shape follows the Daraja STK callback (Body.stkCallback), verified 2026-09-21 against the
portal's M-Pesa Express page. Optional metadata items Balance, TransactionDate and PhoneNumber are
omitted; CallbackMetadata is emitted only for success, as documented.

Callback authenticity here is an HMAC over the body with a fixed, public test key. It is a
stand-in that exercises verify-then-parse; it does not model how Daraja authenticates callbacks.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Protocol

from mpesa import result_codes
from mpesa.errors import (
    CallbackAuthenticityError,
    CallbackMalformedError,
    ChargeDeclinedError,
    MpesaError,
    OutcomeUnknownError,
    UnknownReferenceError,
)
from mpesa.models import (
    CallbackEvent,
    ChargeAccepted,
    ChargeRequest,
    DeclineReason,
    Outcome,
    PaymentStatus,
)

SIGNATURE_HEADER = "X-Fake-Signature"
DEFAULT_SIGNING_KEY = b"fake-adapter-test-key"


class Scenario(str, Enum):
    SUCCESS = "SUCCESS"
    INSUFFICIENT_FUNDS = "INSUFFICIENT_FUNDS"
    USER_CANCELLED = "USER_CANCELLED"
    WRONG_PIN = "WRONG_PIN"
    PROMPT_EXPIRED = "PROMPT_EXPIRED"
    TIMEOUT_NO_CALLBACK = "TIMEOUT_NO_CALLBACK"
    TIMEOUT_QUERY_RESOLVES = "TIMEOUT_QUERY_RESOLVES"
    DELAYED_CALLBACK = "DELAYED_CALLBACK"
    DUPLICATE_CALLBACK = "DUPLICATE_CALLBACK"
    OUT_OF_ORDER_SUCCESS_THEN_FAILURE = "OUT_OF_ORDER_SUCCESS_THEN_FAILURE"
    OUT_OF_ORDER_FAILURE_THEN_SUCCESS = "OUT_OF_ORDER_FAILURE_THEN_SUCCESS"
    INITIATE_TIMEOUT = "INITIATE_TIMEOUT"
    INITIATE_REJECTED = "INITIATE_REJECTED"
    UNRECOGNISED_CODE = "UNRECOGNISED_CODE"


# "254" + 9 digits starting with 0 is not a valid Kenyan mobile number, so none of these can
# be a real subscriber. Any other well-formed MSISDN behaves as SUCCESS.
MAGIC_MSISDNS: dict[str, Scenario] = {
    "254000000001": Scenario.SUCCESS,
    "254000000002": Scenario.INSUFFICIENT_FUNDS,
    "254000000003": Scenario.USER_CANCELLED,
    "254000000004": Scenario.WRONG_PIN,
    "254000000005": Scenario.PROMPT_EXPIRED,
    "254000000006": Scenario.TIMEOUT_NO_CALLBACK,
    "254000000007": Scenario.TIMEOUT_QUERY_RESOLVES,
    "254000000008": Scenario.DELAYED_CALLBACK,
    "254000000009": Scenario.DUPLICATE_CALLBACK,
    "254000000010": Scenario.OUT_OF_ORDER_SUCCESS_THEN_FAILURE,
    "254000000011": Scenario.OUT_OF_ORDER_FAILURE_THEN_SUCCESS,
    "254000000012": Scenario.INITIATE_TIMEOUT,
    "254000000013": Scenario.INITIATE_REJECTED,
    "254000000014": Scenario.UNRECOGNISED_CODE,
}


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
    """One callback the provider would POST to the callback handler."""

    provider_ref: str
    deliver_at: float
    headers: dict[str, str]
    body: bytes


class DuplicateInitiateError(MpesaError):
    """initiate_charge was called twice for one idempotency key.

    A real provider would charge the customer twice, so the fake refuses loudly. Reaching
    this means the idempotency layer above the port is broken (ADR 0006 invariant I5, I11).
    """


@dataclass
class _Charge:
    request: ChargeRequest
    scenario: Scenario
    provider_ref: str
    merchant_request_id: str
    accepted_at: float
    deliveries: list[tuple[float, int, CallbackDelivery]] = field(default_factory=list)
    resolution: tuple[float, PaymentStatus] | None = None


def provider_ref_for(idempotency_key: str) -> str:
    return "fake-co-" + hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()[:16]


def _merchant_request_id_for(idempotency_key: str) -> str:
    return "fake-mr-" + hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()[16:32]


def _receipt_for(provider_ref: str) -> str:
    return "FAKE" + hashlib.sha256(provider_ref.encode("utf-8")).hexdigest()[:8].upper()


def sign(body: bytes, key: bytes = DEFAULT_SIGNING_KEY) -> str:
    return hmac.new(key, body, hashlib.sha256).hexdigest()


class FakeAdapter:
    """In-memory MpesaPort implementation. Drive time with the injected clock."""

    def __init__(self, clock: Clock | None = None, config: FakeAdapterConfig | None = None) -> None:
        self._clock: Clock = clock if clock is not None else ManualClock()
        self._config = config if config is not None else FakeAdapterConfig()
        self._charges: dict[str, _Charge] = {}
        self._initiated_keys: set[str] = set()
        self._delivered: set[tuple[str, int]] = set()
        self._seq = 0
        self.initiate_call_count = 0

    # MpesaPort -----------------------------------------------------------------------------

    def initiate_charge(self, request: ChargeRequest) -> ChargeAccepted:
        self.initiate_call_count += 1
        if request.idempotency_key in self._initiated_keys:
            raise DuplicateInitiateError(
                f"initiate_charge called twice for key {request.idempotency_key}"
            )
        self._initiated_keys.add(request.idempotency_key)

        scenario = MAGIC_MSISDNS.get(request.msisdn, Scenario.SUCCESS)
        if scenario is Scenario.INITIATE_REJECTED:
            raise ChargeDeclinedError(DeclineReason.REJECTED_AT_INITIATION, "fake-rejected")

        ref = provider_ref_for(request.idempotency_key)
        charge = _Charge(
            request=request,
            scenario=scenario,
            provider_ref=ref,
            merchant_request_id=_merchant_request_id_for(request.idempotency_key),
            accepted_at=self._clock.now(),
        )
        self._charges[ref] = charge
        self._schedule(charge)

        if scenario is Scenario.INITIATE_TIMEOUT:
            # The customer was prompted but the caller never learns the reference. The late
            # callback later arrives for a reference the caller does not hold.
            raise OutcomeUnknownError("fake initiate timeout")
        return ChargeAccepted(ref, charge.merchant_request_id)

    def query_status(self, provider_ref: str) -> PaymentStatus:
        charge = self._charges.get(provider_ref)
        if charge is None:
            raise UnknownReferenceError(provider_ref)
        if charge.resolution is not None and charge.resolution[0] <= self._clock.now():
            return charge.resolution[1]
        return PaymentStatus(provider_ref, Outcome.UNKNOWN)

    def parse_callback(self, headers: Mapping[str, str], body: bytes) -> CallbackEvent:
        supplied = {k.lower(): v for k, v in headers.items()}.get(SIGNATURE_HEADER.lower(), "")
        expected = sign(body, self._config.signing_key)
        if not hmac.compare_digest(supplied, expected):
            raise CallbackAuthenticityError("bad or missing fake signature")

        try:
            callback = json.loads(body)["Body"]["stkCallback"]
            provider_ref = str(callback["CheckoutRequestID"])
            raw_code = callback["ResultCode"]
        except (ValueError, KeyError, TypeError) as exc:
            raise CallbackMalformedError("unparseable callback body") from exc

        outcome, reason = result_codes.classify(raw_code)
        receipt: str | None = None
        amount_minor: int | None = None
        try:
            items = callback.get("CallbackMetadata", {}).get("Item", [])
            values = {item["Name"]: item.get("Value") for item in items}
            if "MpesaReceiptNumber" in values:
                receipt = str(values["MpesaReceiptNumber"])
            if "Amount" in values:
                amount_minor = int(Decimal(str(values["Amount"])) * 100)
        except (AttributeError, KeyError, TypeError, ValueError, InvalidOperation) as exc:
            raise CallbackMalformedError("unparseable callback metadata") from exc
        if outcome is Outcome.SUCCEEDED and (receipt is None or amount_minor is None):
            raise CallbackMalformedError("success callback missing receipt or amount")

        return CallbackEvent(
            provider_ref=provider_ref,
            outcome=outcome,
            decline_reason=reason,
            receipt=receipt,
            amount_minor=amount_minor,
            raw_code=str(raw_code),
            payload_sha256=hashlib.sha256(body).hexdigest(),
        )

    # Test and k6 driver API ----------------------------------------------------------------

    def due_callbacks(self) -> list[CallbackDelivery]:
        """Callbacks whose delivery time has been reached and not yet handed out, in order."""
        now = self._clock.now()
        due: list[tuple[float, int, CallbackDelivery]] = []
        for charge in self._charges.values():
            for deliver_at, seq, delivery in charge.deliveries:
                if deliver_at <= now and (charge.provider_ref, seq) not in self._delivered:
                    due.append((deliver_at, seq, delivery))
        due.sort(key=lambda item: (item[0], item[1]))
        for _, seq, delivery in due:
            self._delivered.add((delivery.provider_ref, seq))
        return [delivery for _, _, delivery in due]

    def scheduled_callbacks(self, provider_ref: str) -> list[CallbackDelivery]:
        """Every callback scheduled for a reference, regardless of the clock, in order."""
        charge = self._charges.get(provider_ref)
        if charge is None:
            raise UnknownReferenceError(provider_ref)
        return [delivery for _, _, delivery in sorted(charge.deliveries, key=lambda i: i[:2])]

    # Internals -----------------------------------------------------------------------------

    def _schedule(self, charge: _Charge) -> None:
        cfg = self._config
        t0 = charge.accepted_at
        soon = t0 + cfg.callback_delay_s
        late = t0 + cfg.late_after_s
        ok = result_codes.RESULT_SUCCESS
        cancelled = result_codes.RESULT_USER_CANCELLED
        scenario = charge.scenario

        plan: list[tuple[float, int]] = []
        if scenario is Scenario.SUCCESS:
            plan = [(soon, ok)]
        elif scenario is Scenario.INSUFFICIENT_FUNDS:
            plan = [(soon, result_codes.RESULT_INSUFFICIENT_FUNDS)]
        elif scenario is Scenario.USER_CANCELLED:
            plan = [(soon, cancelled)]
        elif scenario is Scenario.WRONG_PIN:
            plan = [(soon, result_codes.RESULT_WRONG_PIN)]
        elif scenario is Scenario.PROMPT_EXPIRED:
            plan = [(soon, result_codes.RESULT_PROMPT_NOT_ANSWERED)]
        elif scenario is Scenario.DELAYED_CALLBACK:
            plan = [(late, ok)]
        elif scenario is Scenario.DUPLICATE_CALLBACK:
            plan = [(soon, ok)] * cfg.duplicate_count
        elif scenario is Scenario.OUT_OF_ORDER_SUCCESS_THEN_FAILURE:
            plan = [(soon, ok), (soon + 1, cancelled)]
        elif scenario is Scenario.OUT_OF_ORDER_FAILURE_THEN_SUCCESS:
            plan = [(soon, cancelled), (soon + 1, ok)]
        elif scenario is Scenario.INITIATE_TIMEOUT:
            plan = [(late, ok)]
        elif scenario is Scenario.UNRECOGNISED_CODE:
            plan = [(soon, result_codes.RESULT_FAKE_UNRECOGNISED)]
        # TIMEOUT_NO_CALLBACK and TIMEOUT_QUERY_RESOLVES schedule no callback.

        for deliver_at, code in plan:
            self._seq += 1
            charge.deliveries.append(
                (deliver_at, self._seq, self._build_delivery(charge, deliver_at, code))
            )

        if scenario is Scenario.TIMEOUT_QUERY_RESOLVES:
            charge.resolution = (late, self._status_for(charge, ok))
        elif charge.deliveries:
            first_at, _, _ = min(charge.deliveries, key=lambda item: item[:2])
            first_code = plan[0][1]
            charge.resolution = (first_at, self._status_for(charge, first_code))

    def _status_for(self, charge: _Charge, code: int) -> PaymentStatus:
        outcome, reason = result_codes.classify(code)
        receipt = _receipt_for(charge.provider_ref) if outcome is Outcome.SUCCEEDED else None
        return PaymentStatus(charge.provider_ref, outcome, reason, receipt, str(code))

    def _build_delivery(self, charge: _Charge, deliver_at: float, code: int) -> CallbackDelivery:
        callback: dict[str, object] = {
            "MerchantRequestID": charge.merchant_request_id,
            "CheckoutRequestID": charge.provider_ref,
            "ResultCode": code,
            "ResultDesc": f"fake result {code}",
        }
        if code == result_codes.RESULT_SUCCESS:
            amount = charge.request.amount_minor
            major = amount // 100 if amount % 100 == 0 else amount / 100
            callback["CallbackMetadata"] = {
                "Item": [
                    {"Name": "Amount", "Value": major},
                    {"Name": "MpesaReceiptNumber", "Value": _receipt_for(charge.provider_ref)},
                ]
            }
        body = json.dumps({"Body": {"stkCallback": callback}}, sort_keys=True).encode("utf-8")
        headers = {SIGNATURE_HEADER: sign(body, self._config.signing_key)}
        return CallbackDelivery(charge.provider_ref, deliver_at, headers, body)
