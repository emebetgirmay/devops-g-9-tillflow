"""ADR 0006 invariants against the whole service, with the FakeAdapter and a manual clock."""

from __future__ import annotations

import threading
import unittest
from typing import ClassVar

import _bootstrap  # noqa: F401
from helpers import KEY, ServiceTestCase, key
from mpesa import Outcome

from core.states import (
    PAYMENT_LEGAL,
    PAYMENT_TERMINAL,
    IllegalTransition,
    PaymentState,
    check_transition,
)
from core.store import StaleStateError

SUCCESS = "254000000001"
INSUFFICIENT = "254000000002"
CANCELLED = "254000000003"
WRONG_PIN = "254000000004"
PROMPT_EXPIRED = "254000000005"
NO_CALLBACK = "254000000006"
QUERY_RESOLVES = "254000000007"
DELAYED = "254000000008"
DUPLICATE = "254000000009"
SUCCESS_THEN_FAILURE = "254000000010"
FAILURE_THEN_SUCCESS = "254000000011"
INITIATE_TIMEOUT = "254000000012"
INITIATE_REJECTED = "254000000013"
UNRECOGNISED = "254000000014"


class CreatePaymentTest(ServiceTestCase):
    def test_create_is_201_pending_with_a_provider_reference(self) -> None:
        reply = self.pay()
        self.assertEqual(reply.status, 201)
        self.assertEqual(reply.body["state"], "PENDING")
        self.assertTrue(reply.body["checkout_request_id"].startswith("fake-co-"))

    def test_replay_returns_the_original_response_with_200_and_no_second_charge(self) -> None:
        first = self.pay()
        again = self.pay()
        self.assertEqual(again.status, 200)
        self.assertEqual(again.body, first.body)
        self.assertEqual(again.headers.get("Idempotent-Replayed"), "true")
        self.assertEqual(self.adapter.initiate_call_count, 1)
        self.assertEqual(self.count("payments"), 1)

    def test_same_key_different_payload_is_a_conflict_and_sends_nothing(self) -> None:
        self.pay()
        reply = self.pay(amount=999_900)
        self.assertEqual(reply.status, 409)
        self.assertEqual(reply.body["error"], "idempotency_key_payload_mismatch")
        self.assertEqual(self.adapter.initiate_call_count, 1)
        self.assertEqual(self.count("payments"), 1)

    def test_the_key_is_scoped_by_tenant(self) -> None:
        self.pay(key(1), tenant_id="tenant-a")
        other = self.pay(key(1), tenant_id="tenant-b")
        self.assertEqual(other.status, 201)
        self.assertEqual(self.adapter.initiate_call_count, 2)

    def test_missing_or_malformed_key_is_400(self) -> None:
        self.assertEqual(self.call("POST", "/payments", {"x": 1}).status, 400)
        for bad in ("short", "x" * 65, "has space in the key 1"):
            with self.subTest(key=bad):
                self.assertEqual(self.pay(bad).status, 400)

    def test_request_validation(self) -> None:
        cases = [
            {"tenant_id": ""},
            {"msisdn": "0700000000"},
            {"amount": 0},
            {"amount": -5},
            {"amount": 10.5},
            {"amount": True},
            {"account_reference": "x" * 13},
            {"currency": "USD"},
            {"sale_id": "bad id!"},
        ]
        for override in cases:
            with self.subTest(override=override):
                self.assertEqual(self.pay(key(7), **override).status, 400)
        self.assertEqual(self.call("POST", "/payments", [1], {"Idempotency-Key": KEY}).status, 400)
        self.assertEqual(
            self.call("POST", "/payments", b"{nope", {"Idempotency-Key": KEY}).status, 400
        )
        self.assertEqual(self.count("payments"), 0)

    def test_an_in_flight_duplicate_gets_409_and_no_second_charge(self) -> None:
        self.pay()
        with self.app.store.tx() as conn:
            conn.execute("UPDATE idempotency_keys SET status = 'IN_FLIGHT'")
        reply = self.pay()
        self.assertEqual(reply.status, 409)
        self.assertEqual(reply.body["error"], "idempotency_in_flight")
        self.assertIn("Retry-After", reply.headers)
        self.assertEqual(self.adapter.initiate_call_count, 1)

    def test_concurrent_duplicate_requests_produce_one_charge(self) -> None:
        statuses: list[int] = []
        lock = threading.Lock()

        def worker() -> None:
            reply = self.pay()
            with lock:
                statuses.append(reply.status)

        threads = [threading.Thread(target=worker) for _ in range(12)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(statuses), 12)
        self.assertEqual(statuses.count(201), 1)
        self.assertTrue(set(statuses) <= {200, 201, 409})
        self.assertEqual(self.adapter.initiate_call_count, 1)
        self.assertEqual(self.count("payments"), 1)

    def test_only_one_live_payment_per_sale(self) -> None:
        self.assertEqual(self.pay(key(1), sale_id="sale-1").status, 201)
        clash = self.pay(key(2), sale_id="sale-1")
        self.assertEqual(clash.status, 409)
        self.assertEqual(clash.body["error"], "sale_already_has_live_payment")
        self.assertEqual(self.adapter.initiate_call_count, 1)
        self.assertEqual(self.count("idempotency_keys"), 1)

    def test_a_declined_payment_frees_the_sale(self) -> None:
        first = self.pay(key(1), msisdn=CANCELLED, sale_id="sale-1")
        self.advance(2)
        self.deliver()
        self.assertEqual(self.payment(first.body["payment_id"])["state"], "DECLINED")
        self.assertEqual(self.pay(key(2), sale_id="sale-1").status, 201)

    def test_a_synchronous_rejection_is_a_definitive_decline(self) -> None:
        reply = self.pay(msisdn=INITIATE_REJECTED)
        self.assertEqual(reply.status, 201)
        self.assertEqual(reply.body["state"], "DECLINED")
        self.assertEqual(reply.body["decline_reason"], "REJECTED_AT_INITIATION")

    def test_an_initiate_timeout_is_unknown_and_never_retried(self) -> None:
        reply = self.pay(msisdn=INITIATE_TIMEOUT)
        self.assertEqual(reply.body["state"], "UNKNOWN")
        self.assertIsNone(reply.body["checkout_request_id"])
        self.pay(msisdn=INITIATE_TIMEOUT)  # replay
        self.advance(500)
        self.app.payments.sweep()
        self.assertEqual(self.adapter.initiate_call_count, 1)

    def test_get_by_payment_id_and_by_provider_reference(self) -> None:
        reply = self.pay()
        by_id = self.call("GET", "/payments/" + reply.body["payment_id"])
        by_ref = self.call("GET", "/payments/" + reply.body["checkout_request_id"])
        self.assertEqual(by_id.status, 200)
        self.assertEqual(by_id.body, by_ref.body)
        self.assertEqual(by_id.body["msisdn"], "2540****001")
        self.assertEqual(self.call("GET", "/payments/nope").status, 404)


class CallbackTest(ServiceTestCase):
    def created(self, msisdn: str = SUCCESS, idem: str = KEY, **body) -> dict:
        return self.pay(idem, msisdn=msisdn, **body).body

    def test_success_callback_credits_once(self) -> None:
        created = self.created()
        self.advance(2)
        (delivered,) = self.deliver()
        self.assertEqual((delivered["http"], delivered["status"]), (200, "applied"))
        view = self.payment(created["payment_id"])
        self.assertEqual(view["state"], "SUCCEEDED")
        self.assertEqual(view["ledger_entries"], 1)
        self.assertTrue(view["receipt"].startswith("FAKE"))
        self.assertEqual(self.count("outbox", "event_type = 'payment.succeeded'"), 1)

    def test_replays_are_2xx_with_no_second_ledger_entry_event_or_notification(self) -> None:
        created = self.created(msisdn=DUPLICATE)
        self.advance(2)
        delivered = self.deliver()
        self.assertEqual(len(delivered), 3)
        self.assertEqual([d["http"] for d in delivered], [200, 200, 200])
        self.assertEqual([d["status"] for d in delivered], ["applied", "replay", "replay"])
        for _ in range(10):
            (delivery,) = self.adapter.scheduled_callbacks(created["checkout_request_id"])[:1]
            reply = self.call("POST", "/payments/daraja/callback", delivery.body, delivery.headers)
            self.assertEqual((reply.status, reply.body["status"]), (200, "replay"))
        self.assertEqual(self.count("ledger_entries"), 1)
        self.assertEqual(self.count("outbox"), 1)
        self.assertEqual(self.count("provider_callbacks"), 1)

    def test_cancelled_by_user_is_a_decline_with_a_reason(self) -> None:
        created = self.created(CANCELLED)
        self.advance(2)
        self.deliver()
        view = self.payment(created["payment_id"])
        self.assertEqual(view["state"], "DECLINED")
        self.assertEqual(view["decline_reason"], "USER_CANCELLED")
        self.assertEqual(view["ledger_entries"], 0)

    def test_unverified_result_codes_never_become_terminal(self) -> None:
        for n, msisdn in enumerate((INSUFFICIENT, WRONG_PIN, PROMPT_EXPIRED, UNRECOGNISED)):
            with self.subTest(msisdn=msisdn):
                created = self.created(msisdn, key(n))
                self.advance(2)
                self.deliver()
                state = self.payment(created["payment_id"])["state"]
                self.assertEqual(state, "UNKNOWN")

    def test_success_then_a_stale_failure_stays_succeeded(self) -> None:
        created = self.created(SUCCESS_THEN_FAILURE)
        self.advance(5)
        results = [d["status"] for d in self.deliver()]
        self.assertEqual(results, ["applied", "illegal_transition_logged"])
        view = self.payment(created["payment_id"])
        self.assertEqual((view["state"], view["ledger_entries"]), ("SUCCEEDED", 1))
        anomalies = self.rows("SELECT * FROM anomalies WHERE kind = 'illegal_transition'")
        self.assertEqual([a["severity"] for a in anomalies], ["critical"])

    def test_failure_then_a_success_stays_declined_and_is_not_credited(self) -> None:
        created = self.created(FAILURE_THEN_SUCCESS)
        self.advance(5)
        results = [d["status"] for d in self.deliver()]
        self.assertEqual(results, ["applied", "illegal_transition_logged"])
        view = self.payment(created["payment_id"])
        self.assertEqual((view["state"], view["ledger_entries"]), ("DECLINED", 0))
        anomalies = self.rows("SELECT * FROM anomalies WHERE kind = 'illegal_transition'")
        self.assertEqual([a["severity"] for a in anomalies], ["critical"])

    def test_a_tampered_or_unsigned_callback_is_rejected_and_changes_nothing(self) -> None:
        created = self.created()
        (delivery,) = self.adapter.scheduled_callbacks(created["checkout_request_id"])
        tampered = self.call(
            "POST", "/payments/daraja/callback", delivery.body + b" ", delivery.headers
        )
        unsigned = self.call("POST", "/payments/daraja/callback", delivery.body, {})
        self.assertEqual((tampered.status, unsigned.status), (403, 403))
        self.assertEqual(self.payment(created["payment_id"])["state"], "PENDING")

    def test_a_callback_from_a_source_outside_the_allowlist_is_rejected(self) -> None:
        created = self.created()
        (delivery,) = self.adapter.scheduled_callbacks(created["checkout_request_id"])
        reply = self.call(
            "POST",
            "/payments/daraja/callback",
            delivery.body,
            delivery.headers,
            remote="203.0.113.9",
        )
        self.assertEqual(reply.status, 403)
        self.assertEqual(self.payment(created["payment_id"])["state"], "PENDING")

    def test_an_authentic_but_malformed_callback_is_logged_not_applied(self) -> None:
        from mpesa.fake_common import SIGNATURE_HEADER, sign

        body = b'{"Body": {}}'
        reply = self.call("POST", "/payments/daraja/callback", body, {SIGNATURE_HEADER: sign(body)})
        self.assertEqual(reply.status, 400)
        self.assertEqual(self.count("anomalies", "kind = 'malformed_callback'"), 1)

    def test_an_amount_mismatch_goes_to_review_and_is_not_credited(self) -> None:
        created = self.created()
        with self.app.store.tx() as conn:
            conn.execute("UPDATE payments SET amount_minor = 100000")
        self.advance(2)
        (delivered,) = self.deliver()
        self.assertEqual(delivered["status"], "under_review")
        view = self.payment(created["payment_id"])
        self.assertEqual((view["state"], view["ledger_entries"]), ("NEEDS_REVIEW", 0))
        self.assertEqual(self.count("anomalies", "kind = 'needs_review'"), 1)

    def test_a_success_the_status_query_cannot_confirm_is_held_as_unknown(self) -> None:
        created = self.created(DELAYED)
        (delivery,) = self.adapter.scheduled_callbacks(created["checkout_request_id"])
        early = self.call("POST", "/payments/daraja/callback", delivery.body, delivery.headers)
        self.assertEqual(early.body["status"], "unconfirmed")
        view = self.payment(created["payment_id"])
        self.assertEqual((view["state"], view["ledger_entries"]), ("UNKNOWN", 0))
        self.advance(120)
        result = self.call("POST", f"/payments/{created['payment_id']}/reconcile")
        self.assertEqual(result.body["state"], "SUCCEEDED")
        self.assertEqual(self.payment(created["payment_id"])["ledger_entries"], 1)

    def test_a_success_the_status_query_contradicts_is_not_applied(self) -> None:
        created = self.created(FAILURE_THEN_SUCCESS)
        self.advance(1.5)
        second = self.adapter.scheduled_callbacks(created["checkout_request_id"])[1]
        reply = self.call("POST", "/payments/daraja/callback", second.body, second.headers)
        self.assertEqual(reply.body["status"], "contradicted")
        view = self.payment(created["payment_id"])
        self.assertEqual((view["state"], view["ledger_entries"]), ("PENDING", 0))
        self.assertEqual(self.count("anomalies", "kind = 'unconfirmed_success'"), 1)

    def test_a_callback_for_an_unknown_reference_is_stored_not_credited(self) -> None:
        created = self.created(INITIATE_TIMEOUT)
        self.advance(120)
        (delivered,) = self.deliver()
        self.assertEqual((delivered["http"], delivered["status"]), (200, "unmatched"))
        self.assertEqual(self.count("unmatched_callbacks"), 1)
        self.assertEqual(self.count("ledger_entries"), 0)
        self.assertEqual(self.payment(created["payment_id"])["state"], "UNKNOWN")


class NoConfirmationTest(ServiceTestCase):
    settings_overrides: ClassVar[dict] = {"confirm_success_with_query": False}

    def test_the_confirming_query_can_be_switched_off(self) -> None:
        created = self.pay(msisdn=DELAYED).body
        (delivery,) = self.adapter.scheduled_callbacks(created["checkout_request_id"])
        reply = self.call("POST", "/payments/daraja/callback", delivery.body, delivery.headers)
        self.assertEqual(reply.body["status"], "applied")
        self.assertEqual(self.payment(created["payment_id"])["state"], "SUCCEEDED")


class TimeoutTest(ServiceTestCase):
    def test_no_callback_becomes_unknown_never_a_decline_and_is_escalated_not_failed(self) -> None:
        created = self.pay(msisdn=NO_CALLBACK).body
        pid = created["payment_id"]
        self.advance(89)
        self.app.payments.sweep()
        self.assertEqual(self.payment(pid)["state"], "PENDING")
        self.advance(2)
        self.app.payments.sweep()
        self.assertEqual(self.payment(pid)["state"], "UNKNOWN")
        seen = set()
        for _ in range(40):
            self.advance(3700)
            self.app.payments.sweep()
            seen.add(self.payment(pid)["state"])
            if self.payment(pid)["state"] == "NEEDS_REVIEW":
                break
        self.assertEqual(self.payment(pid)["state"], "NEEDS_REVIEW")
        self.assertFalse(seen & {"DECLINED", "EXPIRED", "SUCCEEDED"})
        self.assertEqual(self.adapter.initiate_call_count, 1)
        self.assertEqual(self.count("anomalies", "kind = 'needs_review'"), 1)

    def test_a_late_success_after_a_timeout_credits_exactly_once(self) -> None:
        pid = self.pay(msisdn=DELAYED).body["payment_id"]
        self.advance(91)
        self.app.payments.sweep()
        self.assertEqual(self.payment(pid)["state"], "UNKNOWN")
        self.advance(29)
        (delivered,) = self.deliver()
        self.assertEqual(delivered["status"], "applied")
        self.app.payments.sweep()
        self.call("POST", f"/payments/{pid}/reconcile")
        view = self.payment(pid)
        self.assertEqual((view["state"], view["ledger_entries"]), ("SUCCEEDED", 1))
        self.assertEqual(self.adapter.initiate_call_count, 1)

    def test_reconcile_and_a_late_callback_together_credit_once(self) -> None:
        pid = self.pay(msisdn=DELAYED).body["payment_id"]
        self.advance(91)
        self.app.payments.sweep()
        self.advance(30)
        result = self.call("POST", f"/payments/{pid}/reconcile")
        self.assertEqual(result.body["state"], "SUCCEEDED")
        (delivered,) = self.deliver()
        self.assertEqual(delivered["status"], "replay")
        self.assertEqual(self.payment(pid)["ledger_entries"], 1)

    def test_the_status_query_alone_can_resolve_an_unknown_payment(self) -> None:
        pid = self.pay(msisdn=QUERY_RESOLVES).body["payment_id"]
        self.advance(91)
        self.app.payments.sweep()
        self.advance(200)
        counts = self.app.payments.sweep()
        self.assertEqual(counts["reconciled"], 1)
        view = self.payment(pid)
        self.assertEqual((view["state"], view["ledger_entries"]), ("SUCCEEDED", 1))

    def test_an_unknown_payment_with_no_reference_cannot_be_queried_and_waits_for_review(
        self,
    ) -> None:
        pid = self.pay(msisdn=INITIATE_TIMEOUT).body["payment_id"]
        result = self.call("POST", f"/payments/{pid}/reconcile")
        self.assertEqual(result.body["reason"], "no_provider_reference")
        for _ in range(40):
            self.advance(3700)
            self.app.payments.sweep()
        self.assertEqual(self.payment(pid)["state"], "NEEDS_REVIEW")
        self.assertEqual(self.adapter.initiate_call_count, 1)

    def test_a_crash_after_the_attempt_marker_is_swept_to_unknown_and_never_resent(self) -> None:
        from core.common import fingerprint
        from core.payments import OPERATION

        fp = fingerprint(
            {
                "tenant_id": "tenant-a",
                "sale_id": None,
                "amount_minor": 150000,
                "currency": "KES",
                "msisdn": SUCCESS,
            }
        )
        now = self.clock.now()
        with self.app.store.tx() as conn:
            self.app.store.idem_begin(conn, "tenant-a", OPERATION, KEY, fp, now, 1000)
            conn.execute(
                "INSERT INTO payments (payment_id, tenant_id, idem_key, msisdn, amount_minor,"
                " currency, reference, state, initiate_started_at, created_at, updated_at)"
                " VALUES ('pay_crashed', 'tenant-a', ?, ?, 150000, 'KES', '', 'CREATED', ?, ?, ?)",
                (KEY, SUCCESS, now, now, now),
            )
        self.assertEqual(self.pay().status, 409)  # in flight while nobody has finished it
        self.advance(121)
        counts = self.app.payments.sweep()
        self.assertEqual(counts["created_to_unknown"], 1)
        self.assertEqual(self.payment("pay_crashed")["state"], "UNKNOWN")
        replay = self.pay()
        self.assertEqual((replay.status, replay.body["state"]), (200, "UNKNOWN"))
        self.assertEqual(self.adapter.initiate_call_count, 0)


class StateMachineTest(ServiceTestCase):
    def test_every_transition_outside_the_table_is_illegal(self) -> None:
        for current in PaymentState:
            for target in PaymentState:
                legal = (current, target) in PAYMENT_LEGAL
                with self.subTest(current=current, target=target):
                    if legal:
                        check_transition("payment", current, target)
                    else:
                        with self.assertRaises(IllegalTransition):
                            check_transition("payment", current, target)

    def test_terminal_states_have_no_way_out(self) -> None:
        for terminal in PAYMENT_TERMINAL:
            for target in PaymentState:
                self.assertNotIn((terminal, target), PAYMENT_LEGAL)

    def test_no_outcome_changes_a_terminal_payment(self) -> None:
        pid = self.pay(msisdn=CANCELLED).body["payment_id"]
        self.advance(2)
        self.deliver()
        self.assertEqual(self.payment(pid)["state"], "DECLINED")
        service = self.app.payments
        for outcome in (Outcome.SUCCEEDED, Outcome.EXPIRED, Outcome.UNKNOWN, Outcome.DECLINED):
            with self.subTest(outcome=outcome), self.app.store.tx() as conn:
                row = conn.execute("SELECT * FROM payments WHERE payment_id = ?", (pid,)).fetchone()
                service._apply_outcome(conn, row, outcome, None, "RCPT", "0", "test")
            self.assertEqual(self.payment(pid)["state"], "DECLINED")
        self.assertEqual(self.payment(pid)["ledger_entries"], 0)

    def test_the_state_writer_is_compare_and_swap(self) -> None:
        pid = self.pay().body["payment_id"]
        with self.assertRaises(StaleStateError), self.app.store.tx() as conn:
            self.app.store.transition(
                conn,
                kind="payment",
                table="payments",
                id_column="payment_id",
                record_id=pid,
                current=PaymentState.UNKNOWN,  # it is really PENDING
                target=PaymentState.SUCCEEDED,
                now=0.0,
            )

    def test_the_database_refuses_a_state_outside_the_machine(self) -> None:
        import sqlite3

        pid = self.pay().body["payment_id"]
        with self.assertRaises(sqlite3.IntegrityError), self.app.store.tx() as conn:
            conn.execute("UPDATE payments SET state = 'REVERSED' WHERE payment_id = ?", (pid,))

    def test_a_second_ledger_entry_for_a_provider_reference_is_a_no_op(self) -> None:
        pid = self.pay().body["payment_id"]
        self.advance(2)
        self.deliver()
        with self.app.store.tx() as conn:
            inserted = self.app.store.ledger(
                conn,
                tenant_id="tenant-a",
                record_id=pid,
                entry_type="PAYMENT_CREDIT",
                amount_minor=150000,
                currency="KES",
                provider="mpesa",
                provider_ref=self.payment(pid)["checkout_request_id"],
                receipt="X",
                now=0.0,
            )
        self.assertFalse(inserted)
        self.assertEqual(self.count("ledger_entries"), 1)

    def test_manual_reconcile_of_a_resolved_or_missing_payment(self) -> None:
        pid = self.pay(msisdn=SUCCESS).body["payment_id"]
        result = self.call("POST", f"/payments/{pid}/reconcile")
        self.assertEqual(result.body["reason"], "not_unknown")
        self.assertEqual(self.call("POST", "/payments/nope/reconcile").status, 404)


if __name__ == "__main__":
    unittest.main()
