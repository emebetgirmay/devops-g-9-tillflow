# ADR 0009 - Payments observability for G3

- **Status:** Proposed (G3 draft)
- **Date:** 2026-09-21
- **DRI:** Payments + integrity, Mitingi Joy Chesang (`@chesangJ`)
- **Related:** [SLOs](../slo-error-budgets.md), [runbook](../runbook.md), [architecture](../architecture.md), [threat model](../threat-model.md), [ADR 0002](0002-rds-postgresql.md), [ADR 0005](0005-dual-cicd-lanes.md), [ADR 0006](0006-idempotency-replay.md), [ADR 0008](0008-b2c-payouts.md)

## Context

G3 is the observability and SLO gate. The SLO doc says it requires Grafana views (5 m, 1 h, 28 d) of uptime, the SLO target, budget remaining, burn rate, RED, saturation and business signals, plus Slack alerts that fire and recover, and it leaves capacity numbers as a placeholder until k6 runs against the fake adapter. Dashboards, alert routing and the collector are Platform's; the signals they draw on come from the services.

What exists today:

- **Collector.** Every task is meant to run an ADOT sidecar. Its config in `infra/envs/sandbox/ecs.tf` has **OTLP receivers only** (gRPC 4317, HTTP 4318) and exports traces to X-Ray and metrics to CloudWatch (namespace `TillFlow`). It has no Prometheus receiver.
- **Payments emits nothing.** No metrics, no traces, plain-text request lines and a few JSON anomaly lines on stderr. It is not deployed and CI builds only `pos`.
- **A constraint worth keeping.** `services/payments` is stdlib-only and a test fails on any outbound HTTP client import in the service code (only the operator script `reconcile.py` is exempt), so the code that moves money has no way to reach Safaricom or anything else.
- **Runbook contract.** Every Slack alert must carry environment, service, symptom, SLO impact, observed value, Grafana panel, runbook link, owner and first safe action.
- **Doc mismatch.** The runbook and architecture assume SQS plus a DLQ buffering callbacks. Payments handles callbacks synchronously in the request, with no queue.

## Decision

### 1. Expose metrics at `GET /metrics` in Prometheus text format

Stdlib only, no new dependency and no outbound call. Counters and histograms live in the process; gauges are computed from the database at scrape time, so they are correct after a restart and identical across tasks (aggregate them with `max`, not `sum`). The endpoint must not be reachable from the internet (open question 3). Platform adds a Prometheus scrape of `127.0.0.1:<port>/metrics` to the ADOT config, and the existing `awsemf` exporter carries the metrics to CloudWatch.

### 2. Metric catalogue

Names are `payments_*` (this service covers both charges and payouts). Labels are low-cardinality and fixed. **No tenant, attendant, phone number or record id is ever a label;** those appear only in logs (masked).

| Metric | Type | Labels | Meaning |
|---|---|---|---|
| `payments_http_requests_total` | counter | `route`, `status_class` | Request rate and errors (RED) |
| `payments_http_request_duration_seconds` | histogram | `route` | Latency (RED) |
| `payments_create_results_total` | counter | `operation` (`payment`, `payout`), `result` (`created`, `replayed`, `mismatch`, `in_flight`, `invalid`, `limit`, `disabled`, `conflict`), `state` (state after the call, when created) | Outcome of `POST /payments` and `POST /payouts` |
| `payments_state_transitions_total` | counter | `kind` (`payment`, `disbursement`), `from`, `to` | Every state change |
| `payments_records` | gauge | `kind`, `state` | Rows by state |
| `payments_oldest_age_seconds` | gauge | `kind`, `state` (`PENDING`, `UNKNOWN`, `NEEDS_REVIEW`) | Age of the oldest row in that state |
| `payments_resolution_seconds` | histogram | `kind`, `outcome` | Creation to terminal state; buckets 1, 5, 15, 30, 60, 120, 300, 900, 3600 |
| `payments_callbacks_total` | counter | `kind` (`stk`, `b2c`), `result` (`applied`, `replay`, `unmatched`, `illegal_transition_logged`, `under_review`, `unconfirmed`, `contradicted`, `source_rejected`, `auth_rejected`, `malformed`) | Callback handling |
| `payments_adapter_calls_total` | counter | `op` (`initiate`, `query`, `disburse`, `disburse_query`), `result` (`ok`, `declined`, `unknown`) | Provider health as the adapter sees it |
| `payments_adapter_call_duration_seconds` | histogram | `op` | Provider latency |
| `payments_reconcile_runs_total` | counter | `result` (`ok`, `error`) | Reconcile pass outcomes |
| `payments_reconcile_last_success_timestamp_seconds` | gauge | none | Freshness of the reconcile pass |
| `payments_anomalies_total` | counter | `kind`, `severity` | Illegal transitions, amount mismatches, constraint violations and the like |
| `payments_payouts_enabled` | gauge | none | The payouts kill switch (1 on, 0 tripped) |

### 3. SLIs from those metrics

- **Payments API (SLO: accepted and terminal within 60 s, at least 99.5%).** Two SLIs, because the SLO doc's wording covers both:
  - *Accepted:* good is a create that ends `PENDING` or a definitive `DECLINED`/`FAILED`; bad is a create that ends `UNKNOWN` or a 5xx. From `payments_create_results_total` and `payments_http_requests_total`.
  - *Resolved in time:* good is a terminal state within the window; bad is a row still unresolved past it. From `payments_resolution_seconds` and `payments_oldest_age_seconds`. Genuine business declines are excluded, as the SLO doc says. **The window's start is open** (question 2): on a real STK push the customer's PIN entry may alone exceed 60 s.
- **Commission (SLO: terminal by 06:30 EAT, at least 99%, duplicate disbursement zero).** At 06:30 EAT, the number of disbursements not yet `SUCCEEDED` or `FAILED` (`payments_records`), and any critical anomaly.
- **Duplicate disbursement or charge is a P0.** The database constraints make a real duplicate unrepresentable, so the signal is any `payments_anomalies_total{severity="critical"}`, which counts contradictions and constraint violations.

### 4. Alerts (Payments rows)

Each alert also carries the fields the runbook contract requires; Platform's Slack template fills environment, service, panel and runbook link.

| Signal | Condition | Severity | First safe action | Owner |
|---|---|---|---|---|
| Critical anomaly | `payments_anomalies_total{severity="critical"}` increases | Page | Freeze payouts (kill switch), inspect the anomaly row, never re-send | `@chesangJ` |
| Payouts paused | `payments_payouts_enabled == 0` | Page during the payout window, else Slack | Read the trip reason, fix the cause, re-enable by hand | `@chesangJ` |
| Needs review | `payments_records{state="NEEDS_REVIEW"} > 0` for 15 m | Slack | Resolve with provider evidence (M-PESA Organization Portal); do not auto-fail | `@chesangJ` |
| Unknown too long | `payments_oldest_age_seconds{state="UNKNOWN"}` over 10 m (payments) or 5 m before 06:30 EAT (payouts) | Slack | Run the reconcile pass; leave pending, never resend | `@chesangJ` |
| Reconcile stale | `payments_reconcile_last_success_timestamp_seconds` older than 15 m | Slack | Run reconcile by hand, check the scheduler | `@emebetgirmay` |
| Callback rejections | `source_rejected` or `auth_rejected` rate above baseline for 10 m | Slack | Check the allowlist and gateway forwarding, look for spoofing | `@chesangJ` |
| Provider unknown rate | `payments_adapter_calls_total{result="unknown"}` share above 5% for 10 m | Slack | Check Daraja status; expect more `UNKNOWN` and later reconcile | `@chesangJ` |
| Budget burn | Fast and slow burn per the SLO doc's budget policy | Page or Slack | Per the SLO doc | `@emebetgirmay` |

### 5. Structured logs and trace propagation

- JSON logs on stdout, one line per request and per state change, picked up by the task's `awslogs` group. Fields: `ts`, `level`, `service`, `event`, `trace_id`, `record_kind`, `record_id`, `state`, `result`. No bodies, no phone numbers (masked only), no credentials, no balances.
- `trace_id` comes from an incoming W3C `traceparent` header or is generated, and is echoed as `X-Trace-Id`. The Commission worker sends `traceparent` so one payout run can be followed from Commission to Payments (the threat model asks for `trace_id` in logs). X-Ray spans through an SDK are deferred (Alternatives).

### 6. k6 against the fake adapter

- **Scenarios:** the ADR 0006 outline (steady mix, replay storm, duplicate submit, timeout then late success) plus payouts (aggregated daily run, replayed run, kill switch trip). Stages: smoke, stepped, spike, and a soak of at least 15 minutes.
- **Harness:** run with `FAKE_CLOCK=system` so timeouts elapse in real time, and have k6 loop `POST /_fake/deliver-callbacks` and `POST /_admin/sweep`. Add a fake-build `GET /_admin/invariants` returning the counts k6 asserts at the end: credits equal succeeded payments, no provider reference with more than one ledger entry, no payout key with more than one live disbursement, no payment declined by a timeout.
- **Thresholds:** from the SLO doc: failed requests under 1%, p95 under 500 ms, checks over 99%, CPU under 70%, memory under 75%, bounded queue age.
- **Capacity caveat.** SQLite serialises writers, so throughput measured on it is **not sizing evidence**. Capacity numbers for the SLO doc need the Postgres backend on RDS (ADR 0002); until then k6 proves correctness under concurrency, not headroom.

### 7. Out of scope

Dashboards and Slack rules (Platform), Product's sale-side signals, the real Daraja adapter and its contract test, X-Ray spans.

## Consequences

- Payments keeps zero third-party dependencies and no outbound HTTP client, so the boundary tests from ADR 0004 stay simple.
- Metrics are per task and reset on restart; the database-derived gauges are the reliable ones for budgets and alerts.
- `/metrics` and the `/_fake` and `/_admin` endpoints must be kept off the public path; that is a Platform routing task.
- One more moving part in the ADOT config (a Prometheus scrape) beyond what `pos` uses.
- Capacity numbers will lag the rest of G3 until Postgres exists.

## Alternatives considered

| Option | Rejected because |
|---|---|
| OpenTelemetry SDK exporting OTLP to the sidecar (as `pos` is set up to) | Needs third-party packages and an HTTP exporter, which breaks the no-outbound-client guarantee on the money path. Kept as the fallback if Platform prefers OTLP over a scrape |
| CloudWatch Embedded Metric Format lines written to stdout | Couples the app to one backend and mixes metrics into logs |
| Logs only, with metric filters | Weak for SLO burn, and it makes every alert a text match |
| X-Ray spans now | Adds the SDK dependency; `trace_id` in logs plus `traceparent` propagation covers the tracing G3 asks for |

## Open questions

| # | Question | Owner |
|---|---|---|
| 1 | Prometheus scrape added to the ADOT config, or OTLP from an SDK. This ADR proposes the scrape | `@emebetgirmay` with `@chesangJ` |
| 2 | Where the 60 s window starts for STK (the customer's PIN time), and how it treats payments left `UNKNOWN` (same as ADR 0006 question 8) | `@emebetgirmay` |
| 3 | Keep `/metrics`, `/_admin/*` and `/_fake/*` off the API gateway and the public ALB path | `@emebetgirmay` |
| 4 | A Payments log group, retention and an ECS service definition | `@emebetgirmay` |
| 5 | Whether callbacks need a queue, as the runbook and architecture assume, or the synchronous handler plus reconcile is enough. Daraja documents no retry, so a queue only helps if it sits at the edge | `@chesangJ` with `@emebetgirmay` |
| 6 | Per-tenant views, which cannot be metric labels; they come from logs or the database | `@Moraaalice` |

## Work items (issue-ready)

| ID | Item | Owner | Depends on |
|---|---|---|---|
| G3-1 | `GET /metrics` with the catalogue above, gauges from the database, tests | `@chesangJ` | none |
| G3-2 | JSON logs, `trace_id` and `traceparent` propagation in Payments and the Commission worker | `@chesangJ` | none |
| G3-3 | Fake-build `GET /_admin/invariants` | `@chesangJ` | none |
| G3-4 | k6 scripts and thresholds under `services/payments/k6/`, run against the fake | `@chesangJ` | G3-3 |
| G3-5 | Postgres backend for Payments and re-run k6 for capacity | `@chesangJ` | RDS (ADR 0002) |
| G3-6 | Payments task, log group and CI build and test job | `@emebetgirmay` | none |
| G3-7 | ADOT Prometheus scrape for Payments, kept off the public path | `@emebetgirmay` | G3-1 |
| G3-8 | Grafana panels and Slack rules from sections 3 and 4 | `@emebetgirmay` | G3-1 |
| G3-9 | Finalize the Payments SLI wording and error budget math (questions 2 and 5) | `@emebetgirmay` | none |
| G3-10 | Scheduled reconcile job and runbook entries (kill switch, `NEEDS_REVIEW`, locked API user) | `@emebetgirmay` | none |
