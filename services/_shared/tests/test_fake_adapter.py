"""Determinism and scenario tests for the FakeAdapter (ADR 0004, ADR 0006)."""

from __future__ import annotations

import unittest

from mpesa import (
    MAGIC_MSISDNS,
    CallbackAuthenticityError,
    CallbackDelivery,
    CallbackMalformedError,
    ChargeDeclinedError,
    ChargeRequest,
    DeclineReason,
    DuplicateInitiateError,
    FakeAdapter,
    FakeAdapterConfig,
    ManualClock,
    MpesaPort,
    Outcome,
    OutcomeUnknownError,
    Scenario,
    UnknownReferenceError,
    result_codes,
)
from mpesa.fake_adapter import SIGNATURE_HEADER, provider_ref_for, sign

KEY = "idem-key-0000000001"


def msisdn_for(scenario: Scenario) -> str:
    return next(number for number, s in MAGIC_MSISDNS.items() if s is scenario)


def make(scenario: Scenario, key: str = KEY, amount_minor: int = 150000) -> ChargeRequest:
    return ChargeRequest(
        idempotency_key=key,
        tenant_id="tenant-a",
        msisdn=msisdn_for(scenario),
        amount_minor=amount_minor,
    )


def fresh() -> tuple[FakeAdapter, ManualClock]:
    clock = ManualClock()
    return FakeAdapter(clock=clock), clock


def run_and_collect(scenario: Scenario, horizon_s: float = 1000.0) -> list[CallbackDelivery]:
    adapter, clock = fresh()
    try:
        adapter.initiate_charge(make(scenario))
    except (OutcomeUnknownError, ChargeDeclinedError):
        pass
    clock.advance(horizon_s)
    return adapter.due_callbacks()


class PortConformanceTest(unittest.TestCase):
    def test_fake_adapter_satisfies_port(self) -> None:
        self.assertIsInstance(FakeAdapter(), MpesaPort)


class RequestValidationTest(unittest.TestCase):
    def test_rejects_bad_inputs(self) -> None:
        good = {
            "idempotency_key": KEY,
            "tenant_id": "t",
            "msisdn": "254000000001",
            "amount_minor": 100,
        }
        ChargeRequest(**good)
        ChargeRequest(**{**good, "reference": "x" * 12, "description": "x" * 13})
        for override in (
            {"idempotency_key": "short"},
            {"idempotency_key": "x" * 65},
            {"idempotency_key": "has space in it 1234"},
            {"msisdn": "0700000000"},
            {"amount_minor": 0},
            {"amount_minor": -5},
            {"amount_minor": 10.5},
            {"amount_minor": True},
            {"reference": "x" * 13},
            {"description": "x" * 14},
        ):
            with self.subTest(override=override), self.assertRaises((ValueError, TypeError)):
                ChargeRequest(**{**good, **override})


class DeterminismTest(unittest.TestCase):
    def test_every_scenario_is_deterministic(self) -> None:
        for scenario in Scenario:
            with self.subTest(scenario=scenario):
                first = run_and_collect(scenario)
                second = run_and_collect(scenario)
                self.assertEqual(first, second)

    def test_provider_ref_is_a_pure_function_of_the_key(self) -> None:
        a, _ = fresh()
        b, _ = fresh()
        ref_a = a.initiate_charge(make(Scenario.SUCCESS)).provider_ref
        ref_b = b.initiate_charge(make(Scenario.SUCCESS)).provider_ref
        self.assertEqual(ref_a, ref_b)
        self.assertEqual(ref_a, provider_ref_for(KEY))

    def test_every_scenario_has_a_magic_number(self) -> None:
        self.assertEqual(set(MAGIC_MSISDNS.values()), set(Scenario))

    def test_unlisted_msisdn_defaults_to_success(self) -> None:
        adapter, clock = fresh()
        req = ChargeRequest(KEY, "t", "254711111111", 100)
        adapter.initiate_charge(req)
        clock.advance(10)
        event = adapter.parse_callback(*_first(adapter.due_callbacks()))
        self.assertIs(event.outcome, Outcome.SUCCEEDED)


def _first(deliveries: list[CallbackDelivery]):
    return deliveries[0].headers, deliveries[0].body


class ImmediateOutcomeScenariosTest(unittest.TestCase):
    def check(self, scenario: Scenario, outcome: Outcome, reason: DeclineReason | None) -> None:
        adapter, clock = fresh()
        accepted = adapter.initiate_charge(make(scenario))
        self.assertEqual(adapter.due_callbacks(), [])  # nothing before the delay
        clock.advance(1)
        deliveries = adapter.due_callbacks()
        self.assertEqual(len(deliveries), 1)
        event = adapter.parse_callback(*_first(deliveries))
        self.assertEqual(event.provider_ref, accepted.provider_ref)
        self.assertIs(event.outcome, outcome)
        self.assertIs(event.decline_reason, reason)
        status = adapter.query_status(accepted.provider_ref)
        self.assertIs(status.outcome, outcome)
        self.assertIs(status.decline_reason, reason)

    def test_success(self) -> None:
        adapter, clock = fresh()
        accepted = adapter.initiate_charge(make(Scenario.SUCCESS))
        clock.advance(1)
        event = adapter.parse_callback(*_first(adapter.due_callbacks()))
        self.assertIs(event.outcome, Outcome.SUCCEEDED)
        self.assertEqual(event.amount_minor, 150000)
        self.assertTrue(event.receipt and event.receipt.startswith("FAKE"))
        self.assertEqual(adapter.query_status(accepted.provider_ref).receipt, event.receipt)

    def test_insufficient_funds_code_is_unknown_until_verified(self) -> None:
        self.check(Scenario.INSUFFICIENT_FUNDS, Outcome.UNKNOWN, None)

    def test_user_cancelled(self) -> None:
        self.check(Scenario.USER_CANCELLED, Outcome.DECLINED, DeclineReason.USER_CANCELLED)

    def test_wrong_pin_code_is_unknown_until_verified(self) -> None:
        self.check(Scenario.WRONG_PIN, Outcome.UNKNOWN, None)

    def test_prompt_expired_code_is_unknown_until_verified(self) -> None:
        self.check(Scenario.PROMPT_EXPIRED, Outcome.UNKNOWN, None)

    def test_unrecognised_code_fails_safe_to_unknown(self) -> None:
        self.check(Scenario.UNRECOGNISED_CODE, Outcome.UNKNOWN, None)


class TimeoutScenariosTest(unittest.TestCase):
    def test_timeout_no_callback_never_resolves(self) -> None:
        adapter, clock = fresh()
        accepted = adapter.initiate_charge(make(Scenario.TIMEOUT_NO_CALLBACK))
        clock.advance(10**6)
        self.assertEqual(adapter.due_callbacks(), [])
        self.assertIs(adapter.query_status(accepted.provider_ref).outcome, Outcome.UNKNOWN)

    def test_timeout_query_resolves_after_late_window(self) -> None:
        adapter, clock = fresh()
        accepted = adapter.initiate_charge(make(Scenario.TIMEOUT_QUERY_RESOLVES))
        clock.advance(119)
        self.assertIs(adapter.query_status(accepted.provider_ref).outcome, Outcome.UNKNOWN)
        clock.advance(1)
        self.assertIs(adapter.query_status(accepted.provider_ref).outcome, Outcome.SUCCEEDED)
        self.assertEqual(adapter.due_callbacks(), [])  # resolves by query only

    def test_delayed_callback_arrives_after_late_window(self) -> None:
        adapter, clock = fresh()
        accepted = adapter.initiate_charge(make(Scenario.DELAYED_CALLBACK))
        clock.advance(119)
        self.assertEqual(adapter.due_callbacks(), [])
        self.assertIs(adapter.query_status(accepted.provider_ref).outcome, Outcome.UNKNOWN)
        clock.advance(1)
        deliveries = adapter.due_callbacks()
        self.assertEqual(len(deliveries), 1)
        self.assertIs(adapter.parse_callback(*_first(deliveries)).outcome, Outcome.SUCCEEDED)

    def test_initiate_timeout_raises_unknown_and_late_callback_is_unmatched(self) -> None:
        adapter, clock = fresh()
        with self.assertRaises(OutcomeUnknownError):
            adapter.initiate_charge(make(Scenario.INITIATE_TIMEOUT))
        clock.advance(120)
        deliveries = adapter.due_callbacks()
        self.assertEqual(len(deliveries), 1)
        event = adapter.parse_callback(*_first(deliveries))
        self.assertEqual(event.provider_ref, provider_ref_for(KEY))
        self.assertIs(event.outcome, Outcome.SUCCEEDED)

    def test_initiate_rejected_raises_definitive_decline(self) -> None:
        adapter, clock = fresh()
        with self.assertRaises(ChargeDeclinedError) as ctx:
            adapter.initiate_charge(make(Scenario.INITIATE_REJECTED))
        self.assertIs(ctx.exception.reason, DeclineReason.REJECTED_AT_INITIATION)
        clock.advance(1000)
        self.assertEqual(adapter.due_callbacks(), [])

    def test_unknown_and_declined_are_distinct_error_types(self) -> None:
        self.assertFalse(issubclass(OutcomeUnknownError, ChargeDeclinedError))
        self.assertFalse(issubclass(ChargeDeclinedError, OutcomeUnknownError))


class ReplayScenariosTest(unittest.TestCase):
    def test_duplicate_callback_is_replayed_n_times_byte_identical(self) -> None:
        adapter, clock = fresh()
        adapter.initiate_charge(make(Scenario.DUPLICATE_CALLBACK))
        clock.advance(1)
        deliveries = adapter.due_callbacks()
        self.assertEqual(len(deliveries), 3)
        self.assertEqual(len({d.body for d in deliveries}), 1)
        events = [adapter.parse_callback(d.headers, d.body) for d in deliveries]
        self.assertEqual(len({e.provider_ref for e in events}), 1)
        self.assertEqual(len({e.payload_sha256 for e in events}), 1)

    def test_duplicate_count_is_configurable(self) -> None:
        adapter = FakeAdapter(clock=ManualClock(), config=FakeAdapterConfig(duplicate_count=7))
        ref = adapter.initiate_charge(make(Scenario.DUPLICATE_CALLBACK)).provider_ref
        self.assertEqual(len(adapter.scheduled_callbacks(ref)), 7)

    def _outcomes(self, scenario: Scenario) -> list[Outcome]:
        adapter, clock = fresh()
        adapter.initiate_charge(make(scenario))
        clock.advance(10)
        return [adapter.parse_callback(d.headers, d.body).outcome for d in adapter.due_callbacks()]

    def test_out_of_order_success_then_failure(self) -> None:
        self.assertEqual(
            self._outcomes(Scenario.OUT_OF_ORDER_SUCCESS_THEN_FAILURE),
            [Outcome.SUCCEEDED, Outcome.DECLINED],
        )

    def test_out_of_order_failure_then_success(self) -> None:
        self.assertEqual(
            self._outcomes(Scenario.OUT_OF_ORDER_FAILURE_THEN_SUCCESS),
            [Outcome.DECLINED, Outcome.SUCCEEDED],
        )

    def test_due_callbacks_are_handed_out_once(self) -> None:
        adapter, clock = fresh()
        adapter.initiate_charge(make(Scenario.SUCCESS))
        clock.advance(5)
        self.assertEqual(len(adapter.due_callbacks()), 1)
        self.assertEqual(adapter.due_callbacks(), [])


class CallbackVerificationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.adapter, clock = fresh()
        self.adapter.initiate_charge(make(Scenario.SUCCESS))
        clock.advance(1)
        self.delivery = self.adapter.due_callbacks()[0]

    def test_tampered_body_fails_authenticity(self) -> None:
        with self.assertRaises(CallbackAuthenticityError):
            self.adapter.parse_callback(self.delivery.headers, self.delivery.body + b" ")

    def test_missing_signature_fails_authenticity(self) -> None:
        with self.assertRaises(CallbackAuthenticityError):
            self.adapter.parse_callback({}, self.delivery.body)

    def test_header_name_is_case_insensitive(self) -> None:
        headers = {SIGNATURE_HEADER.lower(): self.delivery.headers[SIGNATURE_HEADER]}
        self.adapter.parse_callback(headers, self.delivery.body)

    def test_signed_garbage_is_malformed_not_authentic_failure(self) -> None:
        for body in (b"not json", b'{"Body": {}}', b'{"Body": {"stkCallback": {"ResultCode": 0}}}'):
            with self.subTest(body=body), self.assertRaises(CallbackMalformedError):
                self.adapter.parse_callback({SIGNATURE_HEADER: sign(body)}, body)


class GuardRailsTest(unittest.TestCase):
    def test_second_initiate_for_same_key_is_refused_and_counted(self) -> None:
        adapter, _ = fresh()
        adapter.initiate_charge(make(Scenario.SUCCESS))
        with self.assertRaises(DuplicateInitiateError):
            adapter.initiate_charge(make(Scenario.SUCCESS))
        self.assertEqual(adapter.initiate_call_count, 2)

    def test_query_unknown_reference(self) -> None:
        adapter, _ = fresh()
        with self.assertRaises(UnknownReferenceError):
            adapter.query_status("fake-co-doesnotexist")

    def test_clock_cannot_go_backwards(self) -> None:
        with self.assertRaises(ValueError):
            ManualClock().advance(-1)


class ScriptedResultCodeTest(unittest.TestCase):
    def scripted(self, raw_code: object):
        adapter, _ = fresh()
        ref = adapter.initiate_charge(make(Scenario.TIMEOUT_NO_CALLBACK)).provider_ref
        adapter.deliver_result_code(ref, raw_code)
        deliveries = adapter.due_callbacks()
        self.assertEqual(len(deliveries), 1)
        event = adapter.parse_callback(deliveries[0].headers, deliveries[0].body)
        return adapter, ref, event

    def test_unverified_and_odd_codes_resolve_to_unknown(self) -> None:
        raw_codes = (
            1,
            2001,
            1037,
            2,
            17,
            26,
            1019,
            1025,
            1050,
            9999,
            -1,
            "0",
            None,
            1.5,
            True,
            False,
        )
        for raw in raw_codes:
            with self.subTest(raw=raw):
                adapter, ref, event = self.scripted(raw)
                self.assertIs(event.outcome, Outcome.UNKNOWN)
                self.assertIsNone(event.decline_reason)
                self.assertIs(adapter.query_status(ref).outcome, Outcome.UNKNOWN)

    def test_verified_codes_resolve_as_documented(self) -> None:
        adapter, ref, event = self.scripted(0)
        self.assertIs(event.outcome, Outcome.SUCCEEDED)
        self.assertEqual(event.amount_minor, 150000)
        self.assertIs(adapter.query_status(ref).outcome, Outcome.SUCCEEDED)
        adapter, ref, event = self.scripted(1032)
        self.assertIs(event.outcome, Outcome.DECLINED)
        self.assertIs(event.decline_reason, DeclineReason.USER_CANCELLED)

    def test_delay_and_errors(self) -> None:
        adapter, clock = fresh()
        ref = adapter.initiate_charge(make(Scenario.TIMEOUT_NO_CALLBACK)).provider_ref
        adapter.deliver_result_code(ref, 0, delay_s=30)
        self.assertEqual(adapter.due_callbacks(), [])
        self.assertIs(adapter.query_status(ref).outcome, Outcome.UNKNOWN)
        clock.advance(30)
        self.assertEqual(len(adapter.due_callbacks()), 1)
        self.assertIs(adapter.query_status(ref).outcome, Outcome.SUCCEEDED)
        with self.assertRaises(UnknownReferenceError):
            adapter.deliver_result_code("fake-co-nope", 0)
        with self.assertRaises(ValueError):
            adapter.deliver_result_code(ref, 0, delay_s=-1)


class ResultCodeMappingTest(unittest.TestCase):
    def test_mapping(self) -> None:
        cases = {
            0: (Outcome.SUCCEEDED, None),
            1032: (Outcome.DECLINED, DeclineReason.USER_CANCELLED),
        }
        for code, expected in cases.items():
            with self.subTest(code=code):
                self.assertEqual(result_codes.classify(code), expected)

    def test_only_verified_codes_are_in_the_table(self) -> None:
        self.assertEqual(
            result_codes.VERIFIED_CODES,
            {0, 1032},
            "add a code only after verifying it against Daraja docs, and update ADR 0006",
        )

    def test_anything_else_is_unknown_never_declined(self) -> None:
        for code in (1, 2001, 1037, 42, -1, 99999, "0", None, 1.0, True, False):
            with self.subTest(code=code):
                self.assertEqual(result_codes.classify(code), (Outcome.UNKNOWN, None))


if __name__ == "__main__":
    unittest.main()
