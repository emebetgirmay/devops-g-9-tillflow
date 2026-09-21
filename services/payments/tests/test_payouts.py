"""ADR 0008 invariants: idempotent, replay-safe B2C payouts, timeouts that never fail or resend."""

from __future__ import annotations

import threading
import unittest
from typing import ClassVar

import _bootstrap  # noqa: F401
from helpers import KEY, ServiceTestCase, key, payout_body
from mpesa import Outcome

from core.payouts import derive_payout_key, originator_id_for

SUCCESS = "254000000101"
INSUFFICIENT = "254000000102"
NOT_REGISTERED = "254000000103"
RECIPIENT_INVALID = "254000000104"
CONFIGURATION = "254000000105"
NO_RESULT = "254000000106"
QUERY_RESOLVES = "254000000107"
DELAYED = "254000000108"
DUPLICATE_RESULT = "254000000109"
SUCCESS_THEN_FAILURE = "254000000110"
FAILURE_THEN_SUCCESS = "254000000111"
DISBURSE_TIMEOUT = "254000000112"
DISBURSE_REJECTED = "254000000113"
DUPLICATE_ORIGINATOR = "254000000114"
UNRECOGNISED = "254000000115"


class CreatePayoutTest(ServiceTestCase):
    def test_create_is_201_pending_with_our_own_provider_id(self) -> None:
        reply = self.payout()
        self.assertEqual(reply.status, 201)
        self.assertEqual(reply.body["state"], "PENDING")
        originator = reply.body["originator_conversation_id"]
        self.assertLessEqual(len(originator), 20)
        self.assertRegex(originator, r"^[A-Z2-7]+$")
        self.assertTrue(reply.body["conversation_id"].startswith("AG_"))

    def test_replay_returns_the_original_response_and_sends_once(self) -> None:
        first = self.payout()
        again = self.payout()
        self.assertEqual((again.status, again.body), (200, first.body))
        self.assertEqual(again.headers.get("Idempotent-Replayed"), "true")
        self.assertEqual(self.adapter.disburse_call_count, 1)
        self.assertEqual(self.count("disbursements"), 1)

    def test_same_key_different_payload_is_a_conflict_and_sends_nothing(self) -> None:
        self.payout()
        reply = self.payout(amount=600_000)
        self.assertEqual(
            (reply.status, reply.body["error"]), (409, "idempotency_key_payload_mismatch")
        )
        self.assertEqual(self.adapter.disburse_call_count, 1)

    def test_a_second_key_for_the_same_payout_cannot_pay_twice(self) -> None:
        first = self.payout(key(1))
        second = self.payout(key(2))
        self.assertEqual(second.status, 409)
        self.assertEqual(second.body["error"], "payout_already_requested")
        self.assertEqual(second.body["disbursement_id"], first.body["disbursement_id"])
        self.assertEqual(self.adapter.disburse_call_count, 1)

    def test_a_different_period_or_attendant_is_a_different_payout(self) -> None:
        self.assertEqual(self.payout(key(1)).status, 201)
        self.assertEqual(self.payout(key(2), payout_period="2026-09-21").status, 201)
        self.assertEqual(self.payout(key(3), attendant_id="att-2").status, 201)
        self.assertEqual(self.adapter.disburse_call_count, 3)

    def test_callers_cannot_supply_the_payout_key(self) -> None:
        reply = self.payout(payout_key="pk_chosen")
        self.assertEqual(reply.status, 400)
        self.assertEqual(self.count("disbursements"), 0)

    def test_the_derivations_are_deterministic_and_bounded(self) -> None:
        a = derive_payout_key("t", "a", "p")
        self.assertEqual(a, derive_payout_key("t", "a", "p"))
        self.assertNotEqual(a, derive_payout_key("t", "a", "q"))
        self.assertNotEqual(a, derive_payout_key("t", "b", "p"))
        self.assertEqual(originator_id_for("dis_1"), originator_id_for("dis_1"))
        self.assertNotEqual(originator_id_for("dis_1"), originator_id_for("dis_2"))
        self.assertEqual(len(originator_id_for("dis_1")), 20)

    def test_validation_and_provider_limits(self) -> None:
        cases = {
            "amount_below_minimum": 500,
            "amount_not_whole_shillings": 150_050,
            "amount_above_ceiling": 25_000_100,
        }
        for error, amount in cases.items():
            with self.subTest(error=error):
                reply = self.payout(key(9), amount=amount)
                self.assertEqual((reply.status, reply.body["error"]), (422, error))
        for override in (
            {"tenant_id": ""},
            {"msisdn": "0700"},
            {"amount": 0},
            {"amount": 1.5},
            {"payout_period": ""},
        ):
            with self.subTest(override=override):
                self.assertEqual(self.payout(key(8), **override).status, 400)
        self.assertEqual(self.count("disbursements"), 0)
        self.assertEqual(self.count("idempotency_keys"), 0)
        self.assertEqual(self.payout(key(7), amount=1_000).status, 201)  # exactly KSh 10

    def test_concurrent_duplicate_requests_send_once(self) -> None:
        statuses: list[int] = []
        lock = threading.Lock()

        def worker() -> None:
            reply = self.payout()
            with lock:
                statuses.append(reply.status)

        threads = [threading.Thread(target=worker) for _ in range(12)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(statuses.count(201), 1)
        self.assertTrue(set(statuses) <= {200, 201, 409})
        self.assertEqual(self.adapter.disburse_call_count, 1)
        self.assertEqual(self.count("disbursements"), 1)

    def test_get_by_any_identifier(self) -> None:
        reply = self.payout().body
        for ident in (
            reply["disbursement_id"],
            reply["originator_conversation_id"],
            reply["conversation_id"],
        ):
            with self.subTest(ident=ident):
                self.assertEqual(self.call("GET", f"/payouts/{ident}").status, 200)
        self.assertEqual(self.call("GET", "/payouts/nope").status, 404)


class ResultTest(ServiceTestCase):
    def test_success_pays_once(self) -> None:
        created = self.payout().body
        self.advance(2)
        (delivered,) = self.deliver()
        self.assertEqual((delivered["http"], delivered["status"]), (200, "applied"))
        view = self.disbursement(created["disbursement_id"])
        self.assertEqual((view["state"], view["ledger_entries"]), ("SUCCEEDED", 1))
        self.assertTrue(view["receipt"].startswith("FAKE"))
        self.assertEqual(self.count("outbox", "event_type = 'payout.succeeded'"), 1)

    def test_replayed_results_have_one_ledger_effect(self) -> None:
        created = self.payout(msisdn=DUPLICATE_RESULT).body
        self.advance(2)
        delivered = self.deliver()
        self.assertEqual([d["status"] for d in delivered], ["applied", "replay", "replay"])
        first = self.adapter.scheduled_disbursement_results(created["originator_conversation_id"])[
            0
        ]
        for _ in range(10):
            reply = self.call("POST", "/payments/daraja/b2c-callback", first.body, first.headers)
            self.assertEqual((reply.status, reply.body["status"]), (200, "replay"))
        self.assertEqual(self.count("ledger_entries"), 1)
        self.assertEqual(self.count("outbox"), 1)

    def test_documented_failures_are_terminal_with_a_reason(self) -> None:
        cases = {
            NOT_REGISTERED: "RECIPIENT_NOT_REGISTERED",
            RECIPIENT_INVALID: "RECIPIENT_INVALID",
        }
        for n, (msisdn, reason) in enumerate(cases.items()):
            with self.subTest(reason=reason):
                created = self.payout(key(n), msisdn=msisdn, attendant_id=f"att-{n}").body
                self.advance(2)
                self.deliver()
                view = self.disbursement(created["disbursement_id"])
                self.assertEqual((view["state"], view["failure_reason"]), ("FAILED", reason))
                self.assertEqual(view["ledger_entries"], 0)
        self.assertEqual(
            self.payout(key(9), attendant_id="att-9").status, 201
        )  # switch not tripped

    def test_insufficient_funds_fails_the_payout_and_pauses_the_run(self) -> None:
        created = self.payout(key(1), msisdn=INSUFFICIENT).body
        self.advance(2)
        self.deliver()
        self.assertEqual(self.disbursement(created["disbursement_id"])["state"], "FAILED")
        blocked = self.payout(key(2), attendant_id="att-2")
        self.assertEqual((blocked.status, blocked.body["error"]), (503, "payouts_disabled"))
        self.assertEqual(self.adapter.disburse_call_count, 1)
        self.assertEqual(self.count("anomalies", "kind = 'payouts_paused'"), 1)

    def test_configuration_errors_trip_the_kill_switch(self) -> None:
        created = self.payout(key(1), msisdn=CONFIGURATION).body
        self.advance(2)
        self.deliver()
        view = self.disbursement(created["disbursement_id"])
        self.assertEqual((view["state"], view["failure_reason"]), ("FAILED", "CONFIGURATION"))
        self.assertEqual(self.payout(key(2), attendant_id="att-2").status, 503)

    def test_an_accepted_request_still_gets_its_original_answer_after_the_switch_trips(
        self,
    ) -> None:
        first = self.payout(key(1), msisdn=CONFIGURATION)
        self.advance(2)
        self.deliver()
        again = self.payout(key(1), msisdn=CONFIGURATION)
        self.assertEqual((again.status, again.body), (200, first.body))

    def test_a_failed_payout_can_be_requested_again_with_a_new_key(self) -> None:
        self.payout(key(1), msisdn=NOT_REGISTERED)
        self.advance(2)
        self.deliver()
        retry = self.payout(key(2), msisdn=SUCCESS)  # an operator's explicit retry
        self.assertEqual(retry.status, 201)
        self.assertEqual(self.count("disbursements"), 2)

    def test_success_then_a_stale_failure_stays_succeeded_and_raises_p0(self) -> None:
        created = self.payout(msisdn=SUCCESS_THEN_FAILURE).body
        self.advance(5)
        self.assertEqual(
            [d["status"] for d in self.deliver()], ["applied", "illegal_transition_logged"]
        )
        view = self.disbursement(created["disbursement_id"])
        self.assertEqual((view["state"], view["ledger_entries"]), ("SUCCEEDED", 1))
        anomalies = self.rows("SELECT * FROM anomalies WHERE kind = 'illegal_transition'")
        self.assertEqual([a["severity"] for a in anomalies], ["critical"])

    def test_failure_then_a_success_stays_failed_and_raises_p0(self) -> None:
        created = self.payout(msisdn=FAILURE_THEN_SUCCESS).body
        self.advance(5)
        self.assertEqual(
            [d["status"] for d in self.deliver()], ["applied", "illegal_transition_logged"]
        )
        view = self.disbursement(created["disbursement_id"])
        self.assertEqual((view["state"], view["ledger_entries"]), ("FAILED", 0))
        anomalies = self.rows("SELECT * FROM anomalies WHERE kind = 'illegal_transition'")
        self.assertEqual([a["severity"] for a in anomalies], ["critical"])

    def test_an_unrecognised_code_is_unknown_not_a_failure(self) -> None:
        created = self.payout(msisdn=UNRECOGNISED).body
        self.advance(2)
        self.deliver()
        self.assertEqual(self.disbursement(created["disbursement_id"])["state"], "UNKNOWN")

    def test_a_synchronous_rejection_is_a_definitive_failure(self) -> None:
        reply = self.payout(msisdn=DISBURSE_REJECTED)
        self.assertEqual((reply.status, reply.body["state"]), (201, "FAILED"))
        self.assertEqual(reply.body["failure_reason"], "REJECTED_AT_INITIATION")

    def test_auth_source_and_malformed_results(self) -> None:
        created = self.payout().body
        (result,) = self.adapter.scheduled_disbursement_results(
            created["originator_conversation_id"]
        )
        path = "/payments/daraja/b2c-callback"
        self.assertEqual(self.call("POST", path, result.body + b" ", result.headers).status, 403)
        self.assertEqual(self.call("POST", path, result.body, {}).status, 403)
        self.assertEqual(
            self.call("POST", path, result.body, result.headers, remote="203.0.113.9").status, 403
        )
        from mpesa.fake_common import SIGNATURE_HEADER, sign

        bad = b'{"Result": {}}'
        self.assertEqual(self.call("POST", path, bad, {SIGNATURE_HEADER: sign(bad)}).status, 400)
        self.assertEqual(self.disbursement(created["disbursement_id"])["state"], "PENDING")

    def test_a_result_for_an_unknown_id_is_stored_not_applied(self) -> None:
        self.adapter.disburse(  # a disbursement the service never made
            __import__("mpesa").DisbursementRequest(
                KEY, "t", "ORPHAN000000000001", SUCCESS, 100_000
            )
        )
        self.advance(2)
        (delivered,) = self.deliver()
        self.assertEqual(delivered["status"], "unmatched")
        self.assertEqual(self.count("unmatched_callbacks", "kind = 'b2c'"), 1)
        self.assertEqual(self.count("ledger_entries"), 0)

    def test_an_amount_mismatch_goes_to_review_and_raises_p0(self) -> None:
        created = self.payout().body
        with self.app.store.tx() as conn:
            conn.execute("UPDATE disbursements SET amount_minor = 400000")
        self.advance(2)
        (delivered,) = self.deliver()
        self.assertEqual(delivered["status"], "under_review")
        view = self.disbursement(created["disbursement_id"])
        self.assertEqual((view["state"], view["ledger_entries"]), ("NEEDS_REVIEW", 0))
        self.assertEqual(
            self.count("anomalies", "kind = 'needs_review' AND severity = 'critical'"), 1
        )

    def test_scripted_codes_outside_the_table_stay_unknown(self) -> None:
        for n, code in enumerate((5, 17, 1037, "R000002", None, 1.5)):
            with self.subTest(code=code):
                created = self.payout(key(n), msisdn=NO_RESULT, attendant_id=f"att-{n}").body
                scripted = self.call(
                    "POST",
                    "/_fake/script-result-code",
                    {"kind": "payout", "id": created["disbursement_id"], "code": code},
                )
                self.assertEqual(scripted.status, 202)
                self.deliver()
                self.assertEqual(self.disbursement(created["disbursement_id"])["state"], "UNKNOWN")


class TimeoutTest(ServiceTestCase):
    def test_a_disburse_timeout_is_unknown_never_failed_and_never_resent(self) -> None:
        reply = self.payout(msisdn=DISBURSE_TIMEOUT)
        self.assertEqual(reply.body["state"], "UNKNOWN")
        self.assertEqual(self.payout(msisdn=DISBURSE_TIMEOUT).status, 200)  # replay
        self.advance(125)
        (delivered,) = self.deliver()  # the late result for the id we chose
        self.assertEqual(delivered["status"], "applied")
        self.app.payouts.sweep()  # nothing left to reconcile, and nothing is resent
        view = self.disbursement(reply.body["disbursement_id"])
        self.assertEqual((view["state"], view["ledger_entries"]), ("SUCCEEDED", 1))
        self.assertEqual(self.adapter.disburse_call_count, 1)

    def test_a_duplicate_originator_answer_is_unknown_then_reconciled_by_id(self) -> None:
        reply = self.payout(msisdn=DUPLICATE_ORIGINATOR)
        self.assertEqual(reply.body["state"], "UNKNOWN")
        self.assertNotEqual(reply.body["state"], "FAILED")
        self.advance(125)
        result = self.call("POST", f"/payouts/{reply.body['disbursement_id']}/reconcile")
        self.assertEqual(result.body["state"], "SUCCEEDED")
        self.assertEqual(self.disbursement(reply.body["disbursement_id"])["ledger_entries"], 1)
        self.assertEqual(self.adapter.disburse_call_count, 1)

    def test_no_result_becomes_unknown_then_review_never_failed(self) -> None:
        pid = self.payout(msisdn=NO_RESULT).body["disbursement_id"]
        self.advance(91)
        self.app.payouts.sweep()
        self.assertEqual(self.disbursement(pid)["state"], "UNKNOWN")
        seen = set()
        for _ in range(40):
            self.advance(3700)
            self.app.payouts.sweep()
            seen.add(self.disbursement(pid)["state"])
            if self.disbursement(pid)["state"] == "NEEDS_REVIEW":
                break
        self.assertEqual(self.disbursement(pid)["state"], "NEEDS_REVIEW")
        self.assertFalse(seen & {"FAILED", "SUCCEEDED"})
        self.assertEqual(self.adapter.disburse_call_count, 1)

    def test_the_status_query_alone_resolves_an_unknown_payout_once(self) -> None:
        pid = self.payout(msisdn=QUERY_RESOLVES).body["disbursement_id"]
        self.advance(91)
        self.app.payouts.sweep()
        self.advance(200)
        counts = self.app.payouts.sweep()
        self.assertEqual(counts["reconciled"], 1)
        self.app.payouts.sweep()
        view = self.disbursement(pid)
        self.assertEqual((view["state"], view["ledger_entries"]), ("SUCCEEDED", 1))

    def test_a_late_result_and_reconcile_together_pay_once(self) -> None:
        pid = self.payout(msisdn=DELAYED).body["disbursement_id"]
        self.advance(91)
        self.app.payouts.sweep()
        self.advance(30)
        self.assertEqual(self.call("POST", f"/payouts/{pid}/reconcile").body["state"], "SUCCEEDED")
        (delivered,) = self.deliver()
        self.assertEqual(delivered["status"], "replay")
        self.assertEqual(self.disbursement(pid)["ledger_entries"], 1)

    def test_a_crash_after_the_attempt_marker_is_swept_to_unknown_and_never_resent(self) -> None:
        from core.common import fingerprint
        from core.payouts import OPERATION

        payout_key = derive_payout_key("tenant-a", "att-1", "2026-09-20")
        body = payout_body()
        fp = fingerprint(
            {
                "tenant_id": "tenant-a",
                "attendant_id": "att-1",
                "payout_period": "2026-09-20",
                "msisdn": body["msisdn"],
                "amount_minor": body["amount"],
                "currency": "KES",
                "payout_key": payout_key,
            }
        )
        now = self.clock.now()
        with self.app.store.tx() as conn:
            self.app.store.idem_begin(conn, "tenant-a", OPERATION, KEY, fp, now, 1000)
            conn.execute(
                "INSERT INTO disbursements (disbursement_id, tenant_id, idem_key, attendant_id,"
                " payout_period, payout_key, msisdn, amount_minor, currency,"
                " originator_conversation_id, state, initiate_started_at, created_at, updated_at)"
                " VALUES ('dis_crashed', 'tenant-a', ?, 'att-1', '2026-09-20', ?, ?, ?, 'KES',"
                " 'CRASHEDORIGINATOR01', 'CREATED', ?, ?, ?)",
                (KEY, payout_key, body["msisdn"], body["amount"], now, now, now),
            )
        self.assertEqual(self.payout().status, 409)
        self.advance(121)
        self.assertEqual(self.app.payouts.sweep()["created_to_unknown"], 1)
        replay = self.payout()
        self.assertEqual((replay.status, replay.body["state"]), (200, "UNKNOWN"))
        self.assertEqual(self.adapter.disburse_call_count, 0)


class KillSwitchConfigTest(ServiceTestCase):
    settings_overrides: ClassVar[dict] = {"payouts_enabled": False, "payout_max_minor": 1_000_000}

    def test_disabled_by_configuration_sends_nothing(self) -> None:
        reply = self.payout()
        self.assertEqual((reply.status, reply.body["error"]), (503, "payouts_disabled"))
        self.assertEqual(self.adapter.disburse_call_count, 0)
        self.assertEqual(self.count("disbursements"), 0)

    def test_the_ceiling_is_configurable_and_never_above_the_provider_maximum(self) -> None:
        self.assertEqual(self.payout(amount=1_000_100).body["error"], "amount_above_ceiling")


class TerminalTest(ServiceTestCase):
    def test_no_outcome_changes_a_terminal_disbursement(self) -> None:
        created = self.payout(msisdn=NOT_REGISTERED).body
        self.advance(2)
        self.deliver()
        service = self.app.payouts
        for outcome in (Outcome.SUCCEEDED, Outcome.FAILED, Outcome.UNKNOWN):
            with self.subTest(outcome=outcome), self.app.store.tx() as conn:
                row = conn.execute(
                    "SELECT * FROM disbursements WHERE disbursement_id = ?",
                    (created["disbursement_id"],),
                ).fetchone()
                service._apply_outcome(conn, row, outcome, None, "RCPT", "0", "test")
            self.assertEqual(self.disbursement(created["disbursement_id"])["state"], "FAILED")
        self.assertEqual(self.disbursement(created["disbursement_id"])["ledger_entries"], 0)


if __name__ == "__main__":
    unittest.main()
