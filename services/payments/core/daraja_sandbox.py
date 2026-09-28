"""Daraja sandbox adapter, B2C only (ADR 0004, ADR 0008). The one file here that talks to Daraja.

Selected with MPESA_ADAPTER=daraja_sandbox in the deployed sandbox only; CI, tests and k6 keep the
FakeAdapter. Base URL and credentials come from the environment (Platform-managed secret
devops-g9/daraja), never from code.

Money-safety rule for disburse(): a failure *before* the payment request is sent (no access token,
amount not whole shillings) is a definitive DisbursementRejectedError, because nothing reached the
provider. Once the request may have been sent, anything but a documented acceptance is
OutcomeUnknownError (or the duplicate-id error, which is also unknown): Payments moves the payout to
UNKNOWN and never resubmits.

Not built yet (ADR 0008 "Still not built"): STK collection through this adapter, the
QueueTimeOutURL handler, and the asynchronous Transaction Status query.
"""

from __future__ import annotations

import base64
import json
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping

from mpesa import b2c_result_codes
from mpesa.errors import (
    CallbackAuthenticityError,
    ChargeDeclinedError,
    DisbursementRejectedError,
    DuplicateOriginatorConversationError,
    OutcomeUnknownError,
    UnknownReferenceError,
)
from mpesa.models import (
    CallbackEvent,
    ChargeAccepted,
    ChargeRequest,
    DeclineReason,
    DisbursementAccepted,
    DisbursementEvent,
    DisbursementRequest,
    DisbursementStatus,
    FailureReason,
    Outcome,
    PaymentStatus,
)

from core.config import DarajaConfig

# (method, url, headers, body, timeout) -> (http status, response body). Raises OSError on
# transport failure. Swapped for a recorder in tests so they never touch the network.
Send = Callable[[str, str, dict[str, str], bytes | None, float], tuple[int, bytes]]

TOKEN_PATH = "/oauth/v1/generate?grant_type=client_credentials"
B2C_PATH = "/mpesa/b2c/v3/paymentrequest"  # ADR 0008, B2C page
RESULT_PATH = "/payments/daraja/b2c-callback"  # routed in app.py
TIMEOUT_PATH = "/payments/daraja/b2c-timeout"  # not handled yet: the sweep covers it (P7)
DUPLICATE_ORIGINATOR = "500.002.1001"  # ADR 0008, documented sample error
TOKEN_REFRESH_MARGIN_S = 60


def urllib_send(
    method: str, url: str, headers: dict[str, str], body: bytes | None, timeout: float
) -> tuple[int, bytes]:
    request = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()


class DarajaSandboxAdapter:
    def __init__(
        self, config: DarajaConfig, send: Send = urllib_send, timeout_s: float = 30.0
    ) -> None:
        self._config = config
        self._send = send
        self._timeout = timeout_s
        self._lock = threading.Lock()
        self._token: str | None = None
        self._token_expires_at = 0.0

    # DisbursementPort ----------------------------------------------------------------------

    def disburse(self, request: DisbursementRequest) -> DisbursementAccepted:
        if request.amount_minor % 100:
            raise DisbursementRejectedError(
                FailureReason.REJECTED_AT_INITIATION, "amount_not_whole_shillings"
            )
        token = self._access_token()  # raises DisbursementRejectedError: nothing sent yet

        cfg = self._config
        payload = {
            "OriginatorConversationID": request.originator_conversation_id,
            "InitiatorName": cfg.b2c_initiator_name,
            "SecurityCredential": cfg.b2c_security_credential,
            "CommandID": "BusinessPayment",
            "Amount": request.amount_minor // 100,
            "PartyA": cfg.b2c_shortcode,
            "PartyB": request.msisdn,
            "Remarks": request.remarks,
            "QueueTimeOutURL": cfg.callback_base_url + TIMEOUT_PATH,
            "ResultURL": cfg.callback_base_url + RESULT_PATH,
        }
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        try:
            status, raw = self._send(
                "POST",
                cfg.base_url + B2C_PATH,
                headers,
                json.dumps(payload).encode(),
                self._timeout,
            )
        except (OSError, ValueError) as exc:  # timeout or transport: may have been sent
            raise OutcomeUnknownError(f"b2c request outcome unknown: {exc!r}") from exc

        reply = _json(raw)
        if status == 200 and str(reply.get("ResponseCode", "")) == "0":
            conversation_id = reply.get("ConversationID")
            if conversation_id:
                return DisbursementAccepted(
                    request.originator_conversation_id, str(conversation_id)
                )
        if str(reply.get("errorCode", "")) == DUPLICATE_ORIGINATOR:
            raise DuplicateOriginatorConversationError(request.originator_conversation_id)
        # ponytail: every other answer is UNKNOWN, even ones that probably mean "not processed";
        # promote specific codes to DisbursementRejectedError only after the sandbox contract test
        # verifies them (ADR 0008 open question 1).
        raise OutcomeUnknownError(
            f"b2c request not acknowledged: http {status}, "
            f"code {reply.get('ResponseCode', reply.get('errorCode'))!r}"
        )

    def query_disbursement_status(self, originator_conversation_id: str) -> DisbursementStatus:
        # ponytail: Transaction Status is asynchronous (ADR 0008 section 5) and its result handler
        # is not built, so this never claims an outcome. Unresolved payouts end in NEEDS_REVIEW
        # after the reconcile window; an operator settles them from portal evidence.
        return DisbursementStatus(originator_conversation_id, Outcome.UNKNOWN)

    def parse_disbursement_result(
        self, headers: Mapping[str, str], body: bytes
    ) -> DisbursementEvent:
        # Daraja signs nothing; authenticity is the documented source-IP allowlist, which
        # PayoutService.handle_result enforces before calling this.
        return b2c_result_codes.parse_result(body)

    # MpesaPort: STK collection is not built for the real adapter ----------------------------

    def initiate_charge(self, request: ChargeRequest) -> ChargeAccepted:
        # Definitive: nothing is sent, so no charge can exist.
        raise ChargeDeclinedError(DeclineReason.REJECTED_AT_INITIATION, "stk_not_built")

    def query_status(self, provider_ref: str) -> PaymentStatus:
        raise UnknownReferenceError(provider_ref)

    def parse_callback(self, headers: Mapping[str, str], body: bytes) -> CallbackEvent:
        raise CallbackAuthenticityError("STK callbacks are not accepted by this adapter")

    # Internals -----------------------------------------------------------------------------

    def _access_token(self) -> str:
        with self._lock:
            if self._token and time.time() < self._token_expires_at:
                return self._token
            cfg = self._config
            basic = base64.b64encode(f"{cfg.consumer_key}:{cfg.consumer_secret}".encode()).decode()
            try:
                status, raw = self._send(
                    "GET",
                    cfg.base_url + TOKEN_PATH,
                    {"Authorization": f"Basic {basic}"},
                    None,
                    self._timeout,
                )
            except (OSError, ValueError) as exc:
                raise DisbursementRejectedError(
                    FailureReason.REJECTED_AT_INITIATION, "token_unreachable"
                ) from exc
            reply = _json(raw)
            token = reply.get("access_token")
            if status != 200 or not token:
                # Bad consumer key or secret: nothing sent. CONFIGURATION trips the kill switch.
                raise DisbursementRejectedError(FailureReason.CONFIGURATION, f"token_http_{status}")
            try:
                ttl = int(reply.get("expires_in", 0))
            except (TypeError, ValueError):
                ttl = 0
            self._token = str(token)
            self._token_expires_at = time.time() + max(ttl - TOKEN_REFRESH_MARGIN_S, 0)
            return self._token


def _json(raw: bytes) -> dict:
    try:
        value = json.loads(raw)
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}
