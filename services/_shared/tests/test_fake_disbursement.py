"""B2C result codes and the fake disbursement scenarios (ADR 0008)."""

from __future__ import annotations

import unittest

from mpesa import (
    MAGIC_RECIPIENTS,
    B2CScenario,
    CallbackAuthenticityError,
    CallbackMalformedError,
    DisbursementPort,
    DisbursementRejectedError,
    DisbursementRequest,
    DuplicateOriginatorConversationError,
    FailureReason,
    FakeAdapter,
    ManualClock,
    Outcome,
    OutcomeUnknownError,
    UnknownReferenceError,
    b2c_result_codes,
)
from mpesa.fake_common import SIGNATURE_HEADER, sign

KEY = "idem-key-0000000001"


def recipient_for(scenario: B2CScenario) -> str:
    return next(number for number, s in MAGIC_RECIPIENTS.items() if s is scenario)


def make(
    scenario: B2CScenario, oid: str = "ORIG0000000000000001", amount_minor: int = 150000
) -> DisbursementRequest:
    return DisbursementRequest(KEY, "tenant-a", oid, recipient_for(scenario), amount_minor)


def fresh() -> tuple[FakeAdapter, ManualClock]:
    clock = ManualClock()
    return FakeAdapter(clock=clock), clock


def parsed(adapter: FakeAdapter, delivery):
    return adapter.parse_disbursement_result(delivery.headers, delivery.body)


class B2CResultCodeTest(unittest.TestCase):
    def test_documented_table(self) -> None:
        cases = {
            0: (Outcome.SUCCEEDED, None),
            1: (Outcome.FAILED, FailureReason.INSUFFICIENT_FUNDS),
            2: (Outcome.FAILED, FailureReason.AMOUNT_TOO_LOW),
            3: (Outcome.FAILED, FailureReason.AMOUNT_TOO_HIGH),
            4: (Outcome.FAILED, FailureReason.RECIPIENT_LIMIT),
            8: (Outcome.FAILED, FailureReason.RECIPIENT_LIMIT),
            11: (Outcome.FAILED, FailureReason.ACCOUNT_STATE),
            21: (Outcome.FAILED, FailureReason.CONFIGURATION),
            2001: (Outcome.FAILED, FailureReason.CONFIGURATION),
            2006: (Outcome.FAILED, FailureReason.ACCOUNT_STATE),
            2028: (Outcome.FAILED, FailureReason.CONFIGURATION),
            2040: (Outcome.FAILED, FailureReason.RECIPIENT_NOT_REGISTERED),
            8006: (Outcome.FAILED, FailureReason.CONFIGURATION),
            "SFC_IC0003": (Outcome.FAILED, FailureReason.RECIPIENT_INVALID),
        }
        for code, expected in cases.items():
            with self.subTest(code=code):
                self.assertEqual(b2c_result_codes.classify_b2c(code), expected)
        self.assertEqual(set(cases), set(b2c_result_codes.VERIFIED_B2C_CODES))

    def test_strings_are_normalised_and_odd_values_are_unknown(self) -> None:
        self.assertEqual(b2c_result_codes.classify_b2c("0")[0], Outcome.SUCCEEDED)
        self.assertEqual(b2c_result_codes.classify_b2c(" 2001 ")[0], Outcome.FAILED)
        for code in (5, 17, 1037, 1032, -1, 99999, "R000002", "", None, 1.5, True, False, [0]):
            with self.subTest(code=code):
                self.assertEqual(b2c_result_codes.classify_b2c(code), (Outcome.UNKNOWN, None))


class RequestValidationTest(unittest.TestCase):
    def test_rejects_bad_inputs(self) -> None:
        good = {
            "idempotency_key": KEY,
            "tenant_id": "t",
            "originator_conversation_id": "ORIG0000000000000001",
            "msisdn": "254000000101",
            "amount_minor": 100,
        }
        DisbursementRequest(**good)
        for override in (
            {"originator_conversation_id": "x" * 21},
            {"originator_conversation_id": ""},
            {"originator_conversation_id": "has space"},
            {"msisdn": "0700000000"},
            {"amount_minor": 0},
            {"amount_minor": 10.5},
            {"amount_minor": True},
            {"remarks": "x"},
        ):
            with self.subTest(override=override), self.assertRaises((ValueError, TypeError)):
                DisbursementRequest(**{**good, **override})


class FakeDisbursementTest(unittest.TestCase):
    def test_conforms_to_port(self) -> None:
        self.assertIsInstance(FakeAdapter(), DisbursementPort)

    def test_every_scenario_has_a_magic_recipient(self) -> None:
        self.assertEqual(set(MAGIC_RECIPIENTS.values()), set(B2CScenario))

    def test_success_and_query_agree(self) -> None:
        adapter, clock = fresh()
        accepted = adapter.disburse(make(B2CScenario.SUCCESS))
        self.assertEqual(adapter.due_disbursement_results(), [])
        clock.advance(1)
        (delivery,) = adapter.due_disbursement_results()
        event = parsed(adapter, delivery)
        self.assertIs(event.outcome, Outcome.SUCCEEDED)
        self.assertEqual(event.amount_minor, 150000)
        self.assertEqual(event.conversation_id, accepted.conversation_id)
        self.assertTrue(event.receipt and event.receipt.startswith("FAKE"))
        status = adapter.query_disbursement_status(accepted.originator_conversation_id)
        self.assertIs(status.outcome, Outcome.SUCCEEDED)
        self.assertEqual(status.receipt, event.receipt)

    def test_documented_failures(self) -> None:
        cases = {
            B2CScenario.INSUFFICIENT_FUNDS: FailureReason.INSUFFICIENT_FUNDS,
            B2CScenario.RECIPIENT_NOT_REGISTERED: FailureReason.RECIPIENT_NOT_REGISTERED,
            B2CScenario.RECIPIENT_INVALID: FailureReason.RECIPIENT_INVALID,
            B2CScenario.CONFIGURATION_ERROR: FailureReason.CONFIGURATION,
        }
        for scenario, reason in cases.items():
            with self.subTest(scenario=scenario):
                adapter, clock = fresh()
                adapter.disburse(make(scenario))
                clock.advance(1)
                (delivery,) = adapter.due_disbursement_results()
                event = parsed(adapter, delivery)
                self.assertIs(event.outcome, Outcome.FAILED)
                self.assertIs(event.failure_reason, reason)
                self.assertIsNone(event.receipt)

    def test_provider_limits(self) -> None:
        for amount, reason in (
            (999, FailureReason.AMOUNT_TOO_LOW),
            (25_000_001, FailureReason.AMOUNT_TOO_HIGH),
        ):
            with self.subTest(amount=amount):
                adapter, clock = fresh()
                adapter.disburse(make(B2CScenario.SUCCESS, amount_minor=amount))
                clock.advance(1)
                event = parsed(adapter, adapter.due_disbursement_results()[0])
                self.assertIs(event.failure_reason, reason)

    def test_repeated_originator_id_is_rejected_as_unknown(self) -> None:
        adapter, _ = fresh()
        adapter.disburse(make(B2CScenario.SUCCESS))
        with self.assertRaises(DuplicateOriginatorConversationError) as ctx:
            adapter.disburse(make(B2CScenario.SUCCESS))
        self.assertIsInstance(ctx.exception, OutcomeUnknownError)
        self.assertEqual(adapter.disburse_call_count, 2)

    def test_duplicate_originator_scenario_resolves_by_id(self) -> None:
        adapter, clock = fresh()
        with self.assertRaises(DuplicateOriginatorConversationError):
            adapter.disburse(make(B2CScenario.DUPLICATE_ORIGINATOR_ERROR))
        self.assertIs(
            adapter.query_disbursement_status("ORIG0000000000000001").outcome, Outcome.UNKNOWN
        )
        clock.advance(120)
        self.assertIs(
            adapter.query_disbursement_status("ORIG0000000000000001").outcome, Outcome.SUCCEEDED
        )

    def test_timeouts_and_rejection(self) -> None:
        adapter, clock = fresh()
        with self.assertRaises(OutcomeUnknownError):
            adapter.disburse(make(B2CScenario.DISBURSE_TIMEOUT))
        clock.advance(120)
        self.assertEqual(len(adapter.due_disbursement_results()), 1)
        self.assertIs(
            adapter.query_disbursement_status("ORIG0000000000000001").outcome, Outcome.SUCCEEDED
        )
        with self.assertRaises(DisbursementRejectedError) as ctx:
            adapter.disburse(make(B2CScenario.DISBURSE_REJECTED, oid="ORIG0000000000000002"))
        self.assertIs(ctx.exception.reason, FailureReason.REJECTED_AT_INITIATION)

    def test_no_result_and_query_only_resolution(self) -> None:
        adapter, clock = fresh()
        adapter.disburse(make(B2CScenario.TIMEOUT_NO_RESULT, oid="ORIG0000000000000001"))
        adapter.disburse(make(B2CScenario.TIMEOUT_QUERY_RESOLVES, oid="ORIG0000000000000002"))
        clock.advance(10**6)
        self.assertEqual(adapter.due_disbursement_results(), [])
        self.assertIs(
            adapter.query_disbursement_status("ORIG0000000000000001").outcome, Outcome.UNKNOWN
        )
        self.assertIs(
            adapter.query_disbursement_status("ORIG0000000000000002").outcome, Outcome.SUCCEEDED
        )

    def test_replay_and_ordering(self) -> None:
        adapter, clock = fresh()
        adapter.disburse(make(B2CScenario.DUPLICATE_RESULT))
        clock.advance(1)
        deliveries = adapter.due_disbursement_results()
        self.assertEqual(len(deliveries), 3)
        self.assertEqual(len({d.body for d in deliveries}), 1)
        adapter, clock = fresh()
        adapter.disburse(make(B2CScenario.OUT_OF_ORDER_FAILURE_THEN_SUCCESS))
        clock.advance(10)
        outcomes = [parsed(adapter, d).outcome for d in adapter.due_disbursement_results()]
        self.assertEqual(outcomes, [Outcome.FAILED, Outcome.SUCCEEDED])

    def test_is_deterministic(self) -> None:
        def run() -> list[bytes]:
            adapter, clock = fresh()
            adapter.disburse(make(B2CScenario.SUCCESS))
            clock.advance(5)
            return [d.body for d in adapter.due_disbursement_results()]

        self.assertEqual(run(), run())

    def test_authenticity_and_malformed(self) -> None:
        adapter, clock = fresh()
        adapter.disburse(make(B2CScenario.SUCCESS))
        clock.advance(1)
        (delivery,) = adapter.due_disbursement_results()
        with self.assertRaises(CallbackAuthenticityError):
            adapter.parse_disbursement_result(delivery.headers, delivery.body + b" ")
        with self.assertRaises(CallbackAuthenticityError):
            adapter.parse_disbursement_result({}, delivery.body)
        for body in (b"not json", b'{"Result": {}}'):
            with self.subTest(body=body), self.assertRaises(CallbackMalformedError):
                adapter.parse_disbursement_result({SIGNATURE_HEADER: sign(body)}, body)

    def test_scripted_codes(self) -> None:
        for raw in (5, 17, 1037, "R000002", None, 1.5, True, 99999):
            with self.subTest(raw=raw):
                adapter, _ = fresh()
                adapter.disburse(make(B2CScenario.TIMEOUT_NO_RESULT))
                adapter.deliver_disbursement_code("ORIG0000000000000001", raw)
                event = parsed(adapter, adapter.due_disbursement_results()[0])
                self.assertIs(event.outcome, Outcome.UNKNOWN)
                status = adapter.query_disbursement_status("ORIG0000000000000001")
                self.assertIs(status.outcome, Outcome.UNKNOWN)
        adapter, _ = fresh()
        with self.assertRaises(UnknownReferenceError):
            adapter.deliver_disbursement_code("nope", 0)
        with self.assertRaises(UnknownReferenceError):
            adapter.query_disbursement_status("nope")


if __name__ == "__main__":
    unittest.main()
