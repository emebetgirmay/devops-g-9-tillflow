# Runbook — TillFlow (stub for G0; complete by G4)

**Owner:** Reliability DRI — Emebet Girmay (`@emebetgirmay`)

## First safe actions (draft)

| Symptom | First safe action | Recovery signal |
|---|---|---|
| Payments callback lag | Check SQS age / DLQ; do not redrive blindly | Callback lag < SLO; Grafana green |
| Uncertain STK (timeout) | Leave pending; run query/reconcile | Terminal paid/failed; no duplicate charge |
| Commission late | Inspect ledger + Payments B2C status; no manual double B2C | Terminal by policy; duplicate = 0 |
| Broken release | Stop promotions; ECS rollback to last good digest | Post-deploy smoke pass |
| Cache down | Expect higher RDS load; scale/repair Redis | p95 recovered; error rate down |

## Alert contract

Every Slack alert must include: environment, service, symptom, user/SLO impact, observed value, Grafana panel, runbook link, owner, first safe action. Webhook only in Secrets Manager (`devops-g9/slack-webhook`).

Path (ADR 0010): CloudWatch alarm, firing and OK, to SNS `devops-g9-alerts`, to Lambda
`devops-g9-slack-notifier`, to Slack. Each alarm's description is JSON with the fields above;
the Lambda formats them and fills gaps from the alarm itself.

## Slack webhook

Terraform creates the secret `devops-g9/slack-webhook` with no value. CI never reads or writes it.
Until a value is set, the notifier logs `slack_webhook_not_set` and drops the message; alarms
still change state.

Set or rotate it by hand, signed in with your own SSO user (`aws sso login --profile g9`), without
leaving the URL in shell history or a file:

```bash
read -rs SLACK_URL   # paste the https://hooks.slack.com/... URL, then Enter
aws secretsmanager put-secret-value --profile g9 --region eu-north-1 \
  --secret-id devops-g9/slack-webhook \
  --secret-string "$(printf '{"url":"%s"}' "$SLACK_URL")"
unset SLACK_URL
```

The notifier only posts to `https://hooks.slack.com/` URLs and reads the secret on every run, so
no deploy is needed after a change. Check: the next alarm's Lambda log shows `slack_posted`
with `status` 200.

## Alarms (G3, ADR 0010)

All alarms post to Slack when they fire and when they recover. Anchors below are the `runbook`
links in each alarm. Never clear an alarm by forcing its state.

### probe-down

The public `/ready` (API Gateway -> VPC link -> ALB -> POS) failed for 2 minutes, or the probe
stopped running.

1. Check `service-down` alarms. If POS is down too, work that first.
2. If POS is healthy behind the ALB, the edge is the problem: API Gateway, the VPC link or the ALB
   listener. Check the latest `Release` run for infra changes.
3. If only the probe is missing data, read `/aws/lambda/devops-g9-probe` logs.

Recovered: `ProbeSuccess` back to 1 for 2 minutes.

### service-down

No healthy POS or Payments target behind the ALB for 2 minutes.

1. ECS -> `devops-g9` -> the service -> stopped tasks: read the stop reason.
2. If a release caused it, roll back to the last good digest (the pipeline's rollback, not a
   hand-made task definition).
3. Payments down: POS keeps sales pending. Do not mark anything paid or failed by hand.

Recovered: `HealthyHostCount` back to at least 1.

<a id="pos-fast-burn"></a><a id="pos-slow-burn"></a>

### pos-fast-burn, pos-slow-burn

POS 5xx share is spending the 99.9% budget 14.4x (fast, pages) or 6x (slow) too quickly.

1. Check POS `/ready` and whether Payments is healthy (a Payments outage shows up here).
2. Look at the POS log group for the failing route.
3. Do not replay sales by hand; clients retry with the same idempotency key.

<a id="payments-fast-burn"></a><a id="payments-slow-burn"></a>

### payments-fast-burn, payments-slow-burn

Payments 5xx share is spending the 99.5% budget too quickly.

1. Find the failing route in the Payments log group.
2. Never resend a payment or payout to clear it; timeouts stay `UNKNOWN` and reconcile.
3. If payouts are affected, the kill switch is the safe stop (ADR 0008).

### ecs-cpu-high

POS or Payments CPU above 70% for 10 minutes.

1. Compare with request rate: real load or a hot loop?
2. Scale out first (desired count), then investigate; roll back only if a release caused it.

### Payments app alarms (ADR 0009 section 4)

These read Payments' own metrics (`GET /metrics`, scraped by ADOT) through Metrics Insights
queries, so they see the service's view rather than the ALB's. Owner `@chesangJ` unless noted.

#### payments-critical-anomaly

A contradiction or constraint violation on the money path. Treat as a P0.

1. Trip the payouts kill switch before anything else.
2. Find the anomaly line in the Payments log group (`event` names the kind); inspect that row.
3. Never re-send a payment or payout to "fix" it; resolve with provider evidence.

#### payments-payouts-paused

The kill switch reads 0. Payouts stop until someone re-enables them.

1. Read the trip reason (the flag's note, and the Payments log around the trip time).
2. Fix the cause (funding, credentials, a stuck result) before re-enabling by hand.

#### payments-needs-review

A payment or payout has sat in `NEEDS_REVIEW` for 15 minutes.

1. Check the provider evidence (M-PESA Organization Portal) for that record.
2. Resolve it by hand as the evidence says. Never auto-fail it.

<a id="payments-payment-unknown-too-long"></a><a id="payments-payout-unknown-too-long"></a>

#### payments-payment-unknown-too-long, payments-payout-unknown-too-long

The oldest `UNKNOWN` payment or payout is older than 10 minutes.

1. Run the reconcile pass (`POST /_admin/sweep` from inside the VPC).
2. Leave it pending if the provider still has no answer. A timeout is not a decline, and a
   payout is never resubmitted (ADR 0006, ADR 0008).

#### payments-reconcile-stale

No successful reconcile pass in 15 minutes. Owner `@emebetgirmay`.

1. Check what calls `POST /_admin/sweep` on a schedule, and its logs.
2. Run one pass by hand from inside the VPC; `payments_reconcile_runs_total{result="error"}`
   rising means the pass itself is failing, so read the Payments log.

## Restore order (G4)

1. Restore DB/S3 to safe target  
2. Verify RPO/RTO  
3. Reconcile Daraja provider references  
4. Declare recovery
