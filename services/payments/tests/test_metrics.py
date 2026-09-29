"""GET /metrics (ADR 0009 sections 1-2, work item G3-1).

Proves: the endpoint returns Prometheus text carrying every named metric;
route labels are templates, never a raw path with an id in it; no tenant,
msisdn, payment or disbursement id ever appears in the output; the
database-computed gauges (records, oldest_age, payouts_enabled,
reconcile_last_success) reflect real rows, not an in-process counter that
would reset on restart; and counters/histograms move the way ADR 0009's
catalogue says they should.
"""

from __future__ import annotations

import unittest

import _bootstrap  # noqa: F401
from helpers import KEY, ServiceTestCase, key, payment_body, payout_body


class MetricsEndpointTest(ServiceTestCase):
    def test_returns_prometheus_text_with_every_named_metric(self) -> None:
        body = self.metrics_text()
        for name in (
            "payments_http_requests_total",
            "payments_http_request_duration_seconds",
            "payments_create_results_total",
            "payments_state_transitions_total",
            "payments_records",
            "payments_oldest_age_seconds",
            "payments_resolution_seconds",
            "payments_callbacks_total",
            "payments_adapter_calls_total",
            "payments_adapter_call_duration_seconds",
            "payments_reconcile_runs_total",
            "payments_reconcile_last_success_timestamp_seconds",
            "payments_anomalies_total",
            "payments_payouts_enabled",
        ):
            self.assertIn(name, body)

    def test_content_type_is_prometheus_text_not_json(self) -> None:
        reply = self.call("GET", "/metrics")
        self.assertEqual(reply.status, 200)
        self.assertIn("text/plain", reply.headers["Content-Type"])
        self.assertIsInstance(reply.body, (bytes, bytearray))

    def test_health_ready_version_metrics_excluded(self) -> None:
        self.call("GET", "/health")
        self.call("GET", "/ready")
        self.call("GET", "/version")
        body = self.metrics_text()
        self.assertNotIn('route="/health"', body)
        self.assertNotIn('route="/ready"', body)
        self.assertNotIn('route="/version"', body)
        self.assertNotIn('route="/metrics"', body)


class RouteLabelTest(ServiceTestCase):
    def test_route_label_is_a_template_not_a_resolved_path(self) -> None:
        first = self.pay(idem=key(1))
        second = self.pay(idem=key(2), tenant_id="tenant-b")
        body = self.metrics_text()
        self.assertIn('route="/payments"', body)
        self.assertNotIn(first.body["payment_id"], body)
        self.assertNotIn(second.body["payment_id"], body)

        self.call("GET", f"/payments/{first.body['payment_id']}")
        self.call("GET", f"/payments/{second.body['payment_id']}")
        body = self.metrics_text()
        self.assertIn('route="/payments/{id}"', body)
        self.assertNotIn(first.body["payment_id"], body)
        self.assertNotIn(second.body["payment_id"], body)

    def test_unmatched_path_is_bucketed_not_leaked(self) -> None:
        suspicious_path = "/tenants/leaked-tenant-id/whatever"
        self.call("GET", suspicious_path)
        body = self.metrics_text()
        self.assertIn('route="unmatched"', body)
        self.assertNotIn("leaked-tenant-id", body)


class NoIdsInLabelsTest(ServiceTestCase):
    def test_no_ids_leak_into_metric_labels_across_a_full_flow(self) -> None:
        pay_reply = self.pay(msisdn="254000000001")
        payment_id = pay_reply.body["payment_id"]
        self.deliver()

        payout_reply = self.payout(idem=key(2), msisdn="254000000101")
        disbursement_id = payout_reply.body["disbursement_id"]
        self.deliver()

        body = self.metrics_text()
        for leaked in (payment_id, disbursement_id, "tenant-a", "254000000001", "254000000101"):
            self.assertNotIn(leaked, body)


class CreateResultsTest(ServiceTestCase):
    def test_payment_create_results_by_outcome(self) -> None:
        self.call("POST", "/payments", payment_body(), None)  # no Idempotency-Key header
        self.pay(idem=key(1))  # created -> PENDING (SUCCESS msisdn is the default in payment_body)
        self.pay(idem=key(1))  # replay of the same key+payload
        self.pay(idem=key(1), amount=999_999)  # same key, different payload -> mismatch

        body = self.metrics_text()
        self.assertIn('payments_create_results_total{operation="payment",result="invalid",state=""} 1', body)
        self.assertIn(
            'payments_create_results_total{operation="payment",result="created",state="PENDING"} 1', body
        )
        self.assertIn(
            'payments_create_results_total{operation="payment",result="replayed",state=""} 1', body
        )
        self.assertIn(
            'payments_create_results_total{operation="payment",result="mismatch",state=""} 1', body
        )

    def test_payout_limit_and_disabled_results(self) -> None:
        self.payout(idem=key(1), amount=1)  # below minimum -> limit
        with self.app.store.tx() as conn:
            self.app.store.set_flag(conn, "payouts_enabled", False, "test", self.clock.now())
        self.payout(idem=key(2))

        body = self.metrics_text()
        self.assertIn('payments_create_results_total{operation="payout",result="limit",state=""} 1', body)
        self.assertIn(
            'payments_create_results_total{operation="payout",result="disabled",state=""} 1', body
        )


class GaugeTest(ServiceTestCase):
    def test_records_and_payouts_enabled_reflect_the_database(self) -> None:
        self.pay(idem=key(1))
        body = self.metrics_text()
        self.assertIn('payments_records{kind="payment",state="PENDING"} 1', body)
        self.assertIn("payments_payouts_enabled 1", body)

        with self.app.store.tx() as conn:
            self.app.store.set_flag(conn, "payouts_enabled", False, "test", self.clock.now())
        body = self.metrics_text()
        self.assertIn("payments_payouts_enabled 0", body)

    def test_oldest_age_seconds_grows_with_the_clock(self) -> None:
        self.pay(idem=key(1))  # PENDING (default msisdn is a FakeAdapter success case)
        self.advance(42)
        body = self.metrics_text()
        self.assertIn('payments_oldest_age_seconds{kind="payment",state="PENDING"} 42', body)

    def test_reconcile_last_success_absent_until_a_pass_runs(self) -> None:
        body = self.metrics_text()
        self.assertNotIn(
            "payments_reconcile_last_success_timestamp_seconds ", self._last_line(body)
        )
        self.call("POST", "/_admin/sweep")
        body = self.metrics_text()
        self.assertIn("payments_reconcile_last_success_timestamp_seconds 1000000", body)

    @staticmethod
    def _last_line(body: str) -> str:
        for line in body.splitlines():
            if line.startswith("payments_reconcile_last_success_timestamp_seconds "):
                return line
        return ""


class ReconcileMetricsTest(ServiceTestCase):
    def test_sweep_records_ok(self) -> None:
        self.call("POST", "/_admin/sweep")
        body = self.metrics_text()
        self.assertIn('payments_reconcile_runs_total{result="ok"} 1', body)


class CallbackMetricsTest(ServiceTestCase):
    def test_disallowed_source_is_recorded(self) -> None:
        self.pay(idem=key(1))
        self.call(
            "POST",
            "/payments/daraja/callback",
            b'{"not":"valid for the fake, but source check runs first"}',
            remote="203.0.113.9",
        )
        body = self.metrics_text()
        self.assertIn('payments_callbacks_total{kind="stk",result="source_rejected"} 1', body)

    def test_applied_callback_is_recorded(self) -> None:
        self.pay(idem=key(1))
        self.advance(2)
        self.deliver()
        body = self.metrics_text()
        self.assertIn('payments_callbacks_total{kind="stk",result="applied"} 1', body)


class StateTransitionAndResolutionMetricsTest(ServiceTestCase):
    def test_state_transitions_and_resolution_recorded_on_success(self) -> None:
        self.pay(idem=key(1))
        self.advance(2)
        self.deliver()
        body = self.metrics_text()
        self.assertIn(
            'payments_state_transitions_total{kind="payment",from_state="CREATED",to_state="PENDING"} 1',
            body,
        )
        self.assertIn(
            'payments_state_transitions_total{kind="payment",from_state="PENDING",to_state="SUCCEEDED"} 1',
            body,
        )
        self.assertIn('payments_resolution_seconds_count{kind="payment",outcome="succeeded"} 1', body)


class AdapterCallMetricsTest(ServiceTestCase):
    def test_initiate_call_recorded(self) -> None:
        self.pay(idem=key(1))
        body = self.metrics_text()
        self.assertIn('payments_adapter_calls_total{op="initiate",result="ok"} 1', body)


if __name__ == "__main__":
    unittest.main()
