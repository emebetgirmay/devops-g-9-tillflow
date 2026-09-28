"""The Daraja sandbox adapter, driven by a recorded transport: no network, no real credentials.

Response shapes follow the Daraja pages ADR 0008 was written against (B2C request acknowledgement,
the 500.002.1001 duplicate error, the Result callback). The live sandbox contract test that
confirms them is separate (ADR 0008 open question 1).
"""

from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

import _bootstrap  # noqa: F401
from helpers import KEY, payout_body
from mpesa import (
    ChargeDeclinedError,
    ChargeRequest,
    DisbursementRejectedError,
    DisbursementRequest,
    DuplicateOriginatorConversationError,
    FailureReason,
    ManualClock,
    Outcome,
    OutcomeUnknownError,
)

from app import App
from core.config import ConfigError, DarajaConfig, Settings
from core.daraja_sandbox import B2C_PATH, TOKEN_PATH, DarajaSandboxAdapter

BASE = "https://sandbox.provider.test"
ENV = {
    "MPESA_ADAPTER": "daraja_sandbox",
    "MPESA_BASE_URL": BASE,
    "MPESA_CONSUMER_KEY": "test-consumer-key",
    "MPESA_CONSUMER_SECRET": "test-consumer-secret",
    "MPESA_B2C_SHORTCODE": "600000",
    "MPESA_B2C_INITIATOR_NAME": "testapi",
    "MPESA_B2C_SECURITY_CREDENTIAL": "test-encrypted-credential",
    "MPESA_CALLBACK_BASE_URL": "https://callbacks.example.test",
}
TOKEN_OK = (200, json.dumps({"access_token": "tok-1", "expires_in": "3599"}).encode())


def b2c_ack(body: bytes) -> tuple[int, bytes]:
    """The documented acknowledgement, echoing the caller's OriginatorConversationID."""
    oid = json.loads(body)["OriginatorConversationID"]
    return 200, json.dumps(
        {
            "ConversationID": "AG_20260928_0001",
            "OriginatorConversationID": oid,
            "ResponseCode": "0",
            "ResponseDescription": "Accept the service request successfully.",
        }
    ).encode()


def result_body(oid: str, code: object = 0, shillings: int = 50) -> bytes:
    result = {
        "ResultType": 0,
        "ResultCode": code,
        "ResultDesc": "The service request is processed successfully.",
        "OriginatorConversationID": oid,
        "ConversationID": "AG_20260928_0001",
        "TransactionID": "SIR0000001",
    }
    if code == 0:
        result["ResultParameters"] = {
            "ResultParameter": [
                {"Key": "TransactionAmount", "Value": shillings},
                {"Key": "TransactionReceipt", "Value": "SIR0000001"},
                {"Key": "ReceiverPartyPublicName", "Value": "254708374149 - Test Recipient"},
            ]
        }
    return json.dumps({"Result": result}).encode()


class Recorder:
    """Stands in for the network. Each scripted answer is a (status, body) pair, an exception to
    raise, or a function of the request body. Every call is recorded."""

    def __init__(self, *answers) -> None:
        self.answers = list(answers)
        self.calls: list[tuple[str, str, dict, bytes | None]] = []

    def __call__(self, method, url, headers, body, timeout):
        self.calls.append((method, url, headers, body))
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer(body) if callable(answer) else answer

    def b2c_calls(self) -> list[dict]:
        return [json.loads(c[3]) for c in self.calls if c[1].endswith(B2C_PATH)]


def request(oid: str = "ORIG0000000000000001", amount_minor: int = 5_000) -> DisbursementRequest:
    return DisbursementRequest(
        idempotency_key=KEY,
        tenant_id="tenant-a",
        originator_conversation_id=oid,
        msisdn="254708374149",
        amount_minor=amount_minor,
    )


def adapter(send: Recorder) -> DarajaSandboxAdapter:
    return DarajaSandboxAdapter(DarajaConfig.from_env(ENV), send=send)


class ConfigTest(unittest.TestCase):
    def test_full_sandbox_settings_select_the_adapter_on_a_system_clock(self) -> None:
        settings = Settings.from_env(ENV)
        self.assertEqual(settings.adapter, "daraja_sandbox")
        self.assertEqual(settings.fake_clock, "system")
        self.assertNotIn("test-consumer-secret", repr(settings))
        self.assertNotIn("test-encrypted-credential", repr(settings))

    def test_every_missing_setting_is_named(self) -> None:
        with self.assertRaises(ConfigError) as ctx:
            Settings.from_env({"MPESA_ADAPTER": "daraja_sandbox"})
        for name in ("MPESA_BASE_URL", "MPESA_CONSUMER_SECRET", "MPESA_B2C_SECURITY_CREDENTIAL"):
            self.assertIn(name, str(ctx.exception))

    def test_only_an_https_sandbox_host_is_accepted(self) -> None:
        for url in ("https://api.provider.test", "http://sandbox.provider.test", "sandbox.x"):
            with self.subTest(url=url), self.assertRaises(ConfigError):
                Settings.from_env({**ENV, "MPESA_BASE_URL": url})
        with self.assertRaises(ConfigError):
            Settings.from_env({**ENV, "MPESA_CALLBACK_BASE_URL": "http://callbacks.test"})


class DisburseTest(unittest.TestCase):
    def test_accepted_request_sends_the_documented_fields_once(self) -> None:
        send = Recorder(TOKEN_OK, b2c_ack)
        accepted = adapter(send).disburse(request())
        self.assertEqual(accepted.conversation_id, "AG_20260928_0001")
        (body,) = send.b2c_calls()
        self.assertEqual(body["Amount"], 50)  # whole shillings, never minor units
        self.assertEqual(body["CommandID"], "BusinessPayment")
        self.assertEqual(body["OriginatorConversationID"], "ORIG0000000000000001")
        self.assertEqual((body["PartyA"], body["PartyB"]), ("600000", "254708374149"))
        self.assertEqual(
            body["ResultURL"], "https://callbacks.example.test/payments/daraja/b2c-callback"
        )
        self.assertEqual(send.calls[1][2]["Authorization"], "Bearer tok-1")

    def test_the_access_token_is_reused(self) -> None:
        send = Recorder(TOKEN_OK, b2c_ack, b2c_ack)
        a = adapter(send)
        a.disburse(request("A0000000000000000001"))
        a.disburse(request("A0000000000000000002"))
        self.assertEqual(sum(c[1].endswith(TOKEN_PATH) for c in send.calls), 1)

    def test_bad_credentials_reject_before_anything_is_sent(self) -> None:
        send = Recorder((400, b'{"errorCode":"400.008.01","errorMessage":"Invalid"}'))
        with self.assertRaises(DisbursementRejectedError) as ctx:
            adapter(send).disburse(request())
        self.assertIs(ctx.exception.reason, FailureReason.CONFIGURATION)
        self.assertEqual(send.b2c_calls(), [])

    def test_unreachable_token_endpoint_rejects_before_anything_is_sent(self) -> None:
        send = Recorder(TimeoutError("token"))
        with self.assertRaises(DisbursementRejectedError):
            adapter(send).disburse(request())
        self.assertEqual(send.b2c_calls(), [])

    def test_a_timeout_after_sending_is_unknown_never_rejected(self) -> None:
        send = Recorder(TOKEN_OK, TimeoutError("read timed out"))
        with self.assertRaises(OutcomeUnknownError) as ctx:
            adapter(send).disburse(request())
        self.assertNotIsInstance(ctx.exception, DisbursementRejectedError)

    def test_the_duplicate_id_error_is_the_duplicate_type(self) -> None:
        dup = (
            500,
            b'{"errorCode":"500.002.1001","errorMessage":"Duplicate OriginatorConversationID."}',
        )
        with self.assertRaises(DuplicateOriginatorConversationError):
            adapter(Recorder(TOKEN_OK, dup)).disburse(request())

    def test_any_other_answer_after_sending_is_unknown(self) -> None:
        for answer in (
            (400, b'{"errorCode":"400.002.02","errorMessage":"Bad Request"}'),
            (200, b'{"ResponseCode":"1","ResponseDescription":"no"}'),
            (200, b'{"ResponseCode":"0"}'),  # acknowledged without a ConversationID
            (503, b"<html>gateway</html>"),
        ):
            with self.subTest(answer=answer), self.assertRaises(OutcomeUnknownError):
                adapter(Recorder(TOKEN_OK, answer)).disburse(request())

    def test_a_fractional_amount_is_refused_without_a_call(self) -> None:
        send = Recorder()
        with self.assertRaises(DisbursementRejectedError):
            adapter(send).disburse(request(amount_minor=5_050))
        self.assertEqual(send.calls, [])


class ResultAndQueryTest(unittest.TestCase):
    def test_success_result_reads_receipt_and_amount_only(self) -> None:
        event = adapter(Recorder()).parse_disbursement_result({}, result_body("ORIG1"))
        self.assertIs(event.outcome, Outcome.SUCCEEDED)
        self.assertEqual((event.receipt, event.amount_minor), ("SIR0000001", 5_000))

    def test_documented_failure_code_is_failed(self) -> None:
        event = adapter(Recorder()).parse_disbursement_result({}, result_body("ORIG1", 2001))
        self.assertIs(event.outcome, Outcome.FAILED)
        self.assertIs(event.failure_reason, FailureReason.CONFIGURATION)

    def test_status_query_never_claims_an_outcome(self) -> None:
        status = adapter(Recorder()).query_disbursement_status("ORIG1")
        self.assertIs(status.outcome, Outcome.UNKNOWN)

    def test_stk_is_refused_without_a_call(self) -> None:
        send = Recorder()
        charge = ChargeRequest(KEY, "tenant-a", "254708374149", 5_000)
        with self.assertRaises(ChargeDeclinedError):
            adapter(send).initiate_charge(charge)
        self.assertEqual(send.calls, [])


class ThroughPaymentsTest(unittest.TestCase):
    """The real PayoutService on top of the adapter: one send per payout, whatever happens."""

    def make_app(self, send: Recorder) -> App:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        settings = replace(Settings.from_env(ENV), db_path=str(Path(tmp.name) / "p.db"))
        return App(settings, clock=ManualClock(1_000_000.0), adapter=adapter(send))

    def post(self, app: App, path: str, body, headers=None):
        raw = body if isinstance(body, bytes) else json.dumps(body).encode()
        return app.dispatch("POST", path, headers or {}, raw, "127.0.0.1")

    def test_accept_then_result_pays_once_and_a_replay_sends_nothing(self) -> None:
        send = Recorder(TOKEN_OK, b2c_ack)
        app = self.make_app(send)
        created = self.post(app, "/payouts", payout_body(), {"Idempotency-Key": KEY})
        self.assertEqual((created.status, created.body["state"]), (201, "PENDING"))
        oid = send.b2c_calls()[0]["OriginatorConversationID"]

        # payout_body() is 500,000 minor = KSh 5,000.
        result = self.post(app, "/payments/daraja/b2c-callback", result_body(oid, 0, 5_000))
        self.assertEqual(result.status, 200)
        again = self.post(app, "/payouts", payout_body(), {"Idempotency-Key": KEY})
        self.assertEqual(again.status, 200)
        view = app.dispatch("GET", f"/payouts/{created.body['disbursement_id']}", {}, b"", "")
        self.assertEqual((view.body["state"], view.body["ledger_entries"]), ("SUCCEEDED", 1))
        self.assertEqual(len(send.b2c_calls()), 1)

    def test_a_timeout_leaves_it_unknown_and_nothing_is_resent(self) -> None:
        send = Recorder(TOKEN_OK, TimeoutError("read timed out"))
        app = self.make_app(send)
        created = self.post(app, "/payouts", payout_body(), {"Idempotency-Key": KEY})
        self.assertEqual(created.body["state"], "UNKNOWN")
        self.post(app, "/payouts", payout_body(), {"Idempotency-Key": KEY})
        self.assertEqual(len(send.b2c_calls()), 1)

    def test_fake_driver_routes_are_gone(self) -> None:
        app = self.make_app(Recorder())
        self.assertEqual(self.post(app, "/_fake/deliver-callbacks", {}).status, 404)


if __name__ == "__main__":
    unittest.main()
