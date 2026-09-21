"""Deterministic fake B2C disbursements for CI, tests and k6 (ADR 0008).

No network, no credentials, no unseeded randomness; time comes from an injected clock. Scenarios
are chosen by magic recipient MSISDNs (see MAGIC_RECIPIENTS and the README). The result payload
follows the documented Daraja B2C result (Result, with ResultParameters only on success), verified
2026-09-21 against the portal's B2C page. The fake omits the recipient name and the account
balances that a real success result carries, on purpose: they are sensitive and not needed.

Documented provider behaviour it models: a repeated OriginatorConversationID is rejected
(DuplicateOriginatorConversationError), and amounts outside the provider limits fail with the
documented result codes 2 and 3. Result authenticity is an HMAC stand-in, as for collections.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from enum import Enum

from mpesa import b2c_result_codes
from mpesa.errors import (
    CallbackAuthenticityError,
    CallbackMalformedError,
    DisbursementRejectedError,
    DuplicateOriginatorConversationError,
    OutcomeUnknownError,
    UnknownReferenceError,
)
from mpesa.fake_common import (
    SIGNATURE_HEADER,
    CallbackDelivery,
    Clock,
    FakeAdapterConfig,
    sign,
)
from mpesa.models import (
    MAX_DISBURSEMENT_MINOR,
    MIN_DISBURSEMENT_MINOR,
    DisbursementAccepted,
    DisbursementEvent,
    DisbursementRequest,
    DisbursementStatus,
    FailureReason,
    Outcome,
)

# Codes emitted by the fake. 99999 is deliberately outside the verified table.
CODE_SUCCESS = 0
CODE_INSUFFICIENT_FUNDS = 1
CODE_AMOUNT_TOO_LOW = 2
CODE_AMOUNT_TOO_HIGH = 3
CODE_NOT_REGISTERED = 2040
CODE_INVALID_INITIATOR = 2001
CODE_RECIPIENT_INVALID = "SFC_IC0003"
CODE_UNRECOGNISED = 99999


class B2CScenario(str, Enum):
    SUCCESS = "SUCCESS"
    INSUFFICIENT_FUNDS = "INSUFFICIENT_FUNDS"
    RECIPIENT_NOT_REGISTERED = "RECIPIENT_NOT_REGISTERED"
    RECIPIENT_INVALID = "RECIPIENT_INVALID"
    CONFIGURATION_ERROR = "CONFIGURATION_ERROR"
    TIMEOUT_NO_RESULT = "TIMEOUT_NO_RESULT"
    TIMEOUT_QUERY_RESOLVES = "TIMEOUT_QUERY_RESOLVES"
    DELAYED_RESULT = "DELAYED_RESULT"
    DUPLICATE_RESULT = "DUPLICATE_RESULT"
    OUT_OF_ORDER_SUCCESS_THEN_FAILURE = "OUT_OF_ORDER_SUCCESS_THEN_FAILURE"
    OUT_OF_ORDER_FAILURE_THEN_SUCCESS = "OUT_OF_ORDER_FAILURE_THEN_SUCCESS"
    DISBURSE_TIMEOUT = "DISBURSE_TIMEOUT"
    DISBURSE_REJECTED = "DISBURSE_REJECTED"
    DUPLICATE_ORIGINATOR_ERROR = "DUPLICATE_ORIGINATOR_ERROR"
    UNRECOGNISED_CODE = "UNRECOGNISED_CODE"


# "254" + 9 digits starting with 0 is not a valid Kenyan mobile number, so none of these can be a
# real subscriber. Any other well-formed MSISDN behaves as SUCCESS.
MAGIC_RECIPIENTS: dict[str, B2CScenario] = {
    "254000000101": B2CScenario.SUCCESS,
    "254000000102": B2CScenario.INSUFFICIENT_FUNDS,
    "254000000103": B2CScenario.RECIPIENT_NOT_REGISTERED,
    "254000000104": B2CScenario.RECIPIENT_INVALID,
    "254000000105": B2CScenario.CONFIGURATION_ERROR,
    "254000000106": B2CScenario.TIMEOUT_NO_RESULT,
    "254000000107": B2CScenario.TIMEOUT_QUERY_RESOLVES,
    "254000000108": B2CScenario.DELAYED_RESULT,
    "254000000109": B2CScenario.DUPLICATE_RESULT,
    "254000000110": B2CScenario.OUT_OF_ORDER_SUCCESS_THEN_FAILURE,
    "254000000111": B2CScenario.OUT_OF_ORDER_FAILURE_THEN_SUCCESS,
    "254000000112": B2CScenario.DISBURSE_TIMEOUT,
    "254000000113": B2CScenario.DISBURSE_REJECTED,
    "254000000114": B2CScenario.DUPLICATE_ORIGINATOR_ERROR,
    "254000000115": B2CScenario.UNRECOGNISED_CODE,
}


class _Disbursement:
    def __init__(self, request: DisbursementRequest, scenario: B2CScenario, accepted_at: float):
        oid = request.originator_conversation_id
        digest = hashlib.sha256(oid.encode("utf-8")).hexdigest()
        self.request = request
        self.scenario = scenario
        self.accepted_at = accepted_at
        self.conversation_id = "AG_" + digest[:16]
        self.receipt = "FAKE" + digest[16:24].upper()
        self.deliveries: list[tuple[float, int, CallbackDelivery]] = []
        self.resolution: tuple[float, DisbursementStatus] | None = None


class FakeDisbursements:
    """In-memory DisbursementPort implementation. Drive time with the injected clock."""

    def __init__(self, clock: Clock, config: FakeAdapterConfig) -> None:
        self._clock = clock
        self._config = config
        self._items: dict[str, _Disbursement] = {}
        self._delivered: set[tuple[str, int]] = set()
        self._seq = 0
        self.disburse_call_count = 0

    # DisbursementPort ----------------------------------------------------------------------

    def disburse(self, request: DisbursementRequest) -> DisbursementAccepted:
        self.disburse_call_count += 1
        oid = request.originator_conversation_id
        if oid in self._items:
            raise DuplicateOriginatorConversationError(f"duplicate originator id {oid}")
        scenario = MAGIC_RECIPIENTS.get(request.msisdn, B2CScenario.SUCCESS)
        if scenario is B2CScenario.DISBURSE_REJECTED:
            raise DisbursementRejectedError(FailureReason.REJECTED_AT_INITIATION, "fake-rejected")

        item = _Disbursement(request, scenario, self._clock.now())
        self._items[oid] = item
        self._schedule(item)

        if scenario is B2CScenario.DISBURSE_TIMEOUT:
            # Money may have moved but the caller never learns it. The late result later arrives
            # for an id the caller does hold (it chose it), so reconciliation can resolve it.
            raise OutcomeUnknownError("fake disburse timeout")
        if scenario is B2CScenario.DUPLICATE_ORIGINATOR_ERROR:
            # An earlier attempt with this id exists and paid; this call is rejected as a repeat.
            raise DuplicateOriginatorConversationError(f"duplicate originator id {oid}")
        return DisbursementAccepted(oid, item.conversation_id)

    def query_disbursement_status(self, originator_conversation_id: str) -> DisbursementStatus:
        item = self._items.get(originator_conversation_id)
        if item is None:
            raise UnknownReferenceError(originator_conversation_id)
        if item.resolution is not None and item.resolution[0] <= self._clock.now():
            return item.resolution[1]
        return DisbursementStatus(originator_conversation_id, Outcome.UNKNOWN)

    def parse_disbursement_result(
        self, headers: Mapping[str, str], body: bytes
    ) -> DisbursementEvent:
        supplied = {k.lower(): v for k, v in headers.items()}.get(SIGNATURE_HEADER.lower(), "")
        if not hmac.compare_digest(supplied, sign(body, self._config.signing_key)):
            raise CallbackAuthenticityError("bad or missing fake signature")

        try:
            result = json.loads(body)["Result"]
            oid = str(result["OriginatorConversationID"])
            raw_code = result["ResultCode"]
            conversation_id = result.get("ConversationID")
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise CallbackMalformedError("unparseable result body") from exc

        outcome, reason = b2c_result_codes.classify_b2c(raw_code)
        receipt: str | None = None
        amount_minor: int | None = None
        try:
            params = result.get("ResultParameters", {}).get("ResultParameter", [])
            values = {item["Key"]: item.get("Value") for item in params}
            if "TransactionReceipt" in values:
                receipt = str(values["TransactionReceipt"])
            if "TransactionAmount" in values:
                amount_minor = int(Decimal(str(values["TransactionAmount"])) * 100)
        except (AttributeError, KeyError, TypeError, ValueError, InvalidOperation) as exc:
            raise CallbackMalformedError("unparseable result parameters") from exc
        if outcome is Outcome.SUCCEEDED and (receipt is None or amount_minor is None):
            raise CallbackMalformedError("success result missing receipt or amount")

        return DisbursementEvent(
            originator_conversation_id=oid,
            conversation_id=None if conversation_id is None else str(conversation_id),
            outcome=outcome,
            failure_reason=reason,
            receipt=receipt,
            amount_minor=amount_minor,
            raw_code=str(raw_code),
            payload_sha256=hashlib.sha256(body).hexdigest(),
        )

    # Test and k6 driver API ----------------------------------------------------------------

    def due_results(self) -> list[CallbackDelivery]:
        """Results whose delivery time has been reached and not yet handed out, in order."""
        now = self._clock.now()
        due: list[tuple[float, int, CallbackDelivery]] = []
        for oid, item in self._items.items():
            for deliver_at, seq, delivery in item.deliveries:
                if deliver_at <= now and (oid, seq) not in self._delivered:
                    due.append((deliver_at, seq, delivery))
        due.sort(key=lambda entry: (entry[0], entry[1]))
        for _, seq, delivery in due:
            self._delivered.add((delivery.provider_ref, seq))
        return [delivery for _, _, delivery in due]

    def scheduled_results(self, originator_conversation_id: str) -> list[CallbackDelivery]:
        item = self._items.get(originator_conversation_id)
        if item is None:
            raise UnknownReferenceError(originator_conversation_id)
        return [d for _, _, d in sorted(item.deliveries, key=lambda entry: entry[:2])]

    def deliver_result_code(
        self, originator_conversation_id: str, raw_code: object, delay_s: float = 0.0
    ) -> CallbackDelivery:
        """Test-only: schedule a signed result carrying any raw ResultCode for a known id, so
        tests can prove codes outside the verified table resolve to UNKNOWN."""
        if delay_s < 0:
            raise ValueError("delay_s must not be negative")
        item = self._items.get(originator_conversation_id)
        if item is None:
            raise UnknownReferenceError(originator_conversation_id)
        deliver_at = self._clock.now() + delay_s
        delivery = self._build_delivery(item, deliver_at, raw_code)
        self._seq += 1
        item.deliveries.append((deliver_at, self._seq, delivery))
        item.resolution = (deliver_at, self._status_for(item, raw_code))
        return delivery

    # Internals -----------------------------------------------------------------------------

    def _schedule(self, item: _Disbursement) -> None:
        cfg = self._config
        t0 = item.accepted_at
        soon = t0 + cfg.callback_delay_s
        late = t0 + cfg.late_after_s
        amount = item.request.amount_minor
        scenario = item.scenario

        plan: list[tuple[float, object]] = []
        if amount < MIN_DISBURSEMENT_MINOR:
            plan = [(soon, CODE_AMOUNT_TOO_LOW)]
        elif amount > MAX_DISBURSEMENT_MINOR:
            plan = [(soon, CODE_AMOUNT_TOO_HIGH)]
        elif scenario is B2CScenario.SUCCESS:
            plan = [(soon, CODE_SUCCESS)]
        elif scenario is B2CScenario.INSUFFICIENT_FUNDS:
            plan = [(soon, CODE_INSUFFICIENT_FUNDS)]
        elif scenario is B2CScenario.RECIPIENT_NOT_REGISTERED:
            plan = [(soon, CODE_NOT_REGISTERED)]
        elif scenario is B2CScenario.RECIPIENT_INVALID:
            plan = [(soon, CODE_RECIPIENT_INVALID)]
        elif scenario is B2CScenario.CONFIGURATION_ERROR:
            plan = [(soon, CODE_INVALID_INITIATOR)]
        elif scenario is B2CScenario.DELAYED_RESULT:
            plan = [(late, CODE_SUCCESS)]
        elif scenario is B2CScenario.DUPLICATE_RESULT:
            plan = [(soon, CODE_SUCCESS)] * cfg.duplicate_count
        elif scenario is B2CScenario.OUT_OF_ORDER_SUCCESS_THEN_FAILURE:
            plan = [(soon, CODE_SUCCESS), (soon + 1, CODE_INSUFFICIENT_FUNDS)]
        elif scenario is B2CScenario.OUT_OF_ORDER_FAILURE_THEN_SUCCESS:
            plan = [(soon, CODE_INSUFFICIENT_FUNDS), (soon + 1, CODE_SUCCESS)]
        elif scenario in (B2CScenario.DISBURSE_TIMEOUT, B2CScenario.DUPLICATE_ORIGINATOR_ERROR):
            plan = [(late, CODE_SUCCESS)]
        elif scenario is B2CScenario.UNRECOGNISED_CODE:
            plan = [(soon, CODE_UNRECOGNISED)]
        # TIMEOUT_NO_RESULT and TIMEOUT_QUERY_RESOLVES schedule no result.

        for deliver_at, code in plan:
            self._seq += 1
            item.deliveries.append(
                (deliver_at, self._seq, self._build_delivery(item, deliver_at, code))
            )

        if scenario is B2CScenario.TIMEOUT_QUERY_RESOLVES:
            item.resolution = (late, self._status_for(item, CODE_SUCCESS))
        elif plan:
            item.resolution = (plan[0][0], self._status_for(item, plan[0][1]))

    def _status_for(self, item: _Disbursement, code: object) -> DisbursementStatus:
        outcome, reason = b2c_result_codes.classify_b2c(code)
        receipt = item.receipt if outcome is Outcome.SUCCEEDED else None
        return DisbursementStatus(
            item.request.originator_conversation_id, outcome, reason, receipt, str(code)
        )

    def _build_delivery(
        self, item: _Disbursement, deliver_at: float, code: object
    ) -> CallbackDelivery:
        oid = item.request.originator_conversation_id
        result: dict[str, object] = {
            "ResultType": 0,
            "ResultCode": code,
            "ResultDesc": f"fake result {code}",
            "OriginatorConversationID": oid,
            "ConversationID": item.conversation_id,
            "TransactionID": item.receipt,
        }
        if type(code) is int and code == CODE_SUCCESS:
            amount = item.request.amount_minor
            major = amount // 100 if amount % 100 == 0 else amount / 100
            result["ResultParameters"] = {
                "ResultParameter": [
                    {"Key": "TransactionAmount", "Value": major},
                    {"Key": "TransactionReceipt", "Value": item.receipt},
                ]
            }
        body = json.dumps({"Result": result}, sort_keys=True).encode("utf-8")
        headers = {SIGNATURE_HEADER: sign(body, self._config.signing_key)}
        return CallbackDelivery(oid, deliver_at, headers, body)
