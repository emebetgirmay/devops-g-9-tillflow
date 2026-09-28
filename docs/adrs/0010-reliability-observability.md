# ADR 0010 - Reliability and observability for G3

- **Status:** Proposed (G3 draft)
- **Date:** 2026-09-28
- **DRI:** Reliability + operations, Emebet Girmay (`@emebetgirmay`)
- **Related:** [SLOs](../slo-error-budgets.md), [runbook](../runbook.md), [architecture](../architecture.md), [ADR 0005](0005-dual-cicd-lanes.md), [ADR 0008](0008-b2c-payouts.md), [ADR 0009](0009-payments-observability.md)

## Context

G3 needs Grafana views (5 m, 1 h, 28 d) of uptime, SLO target, budget remaining, burn rate, RED,
saturation and business signals, Slack alerts that fire and recover, and a k6 run against the fake
adapter ([SLO doc](../slo-error-budgets.md), "Next gate"). ADR 0009 decides how Payments exposes
its signals. This ADR decides everything else: where Grafana runs, where each service's signals
come from, the alert table and its Slack path, how k6 reaches the services, and what the evidence
looks like.

What exists today:

- **Collector.** POS and Payments tasks run an ADOT sidecar with OTLP receivers, exporting to
  X-Ray and CloudWatch EMF (namespace `TillFlow`). No service sends it anything yet.
- **Platform metrics, free.** The internal ALB publishes per-target-group request count, 5xx and
  response time. ECS Container Insights is on (`aws_ecs_cluster.main`), so CPU and memory per
  service exist.
- **No Grafana, no alarms, no Slack path, no k6.** The CI deploy role cannot create SNS, Lambda,
  Secrets Manager, KMS or EventBridge resources.
- **Commission is batch.** `close.py` and `disburse.py` run and exit; each prints one JSON summary
  line. Nothing can scrape them.
- **Cohort experience.** Amazon Managed Grafana in the cohort accounts has been blocked at login:
  IAM Identity Center user assignment is denied to group roles, and a workspace left behind could
  not be deleted later, which broke a release pipeline.

## Decision

### 1. Grafana Cloud, dashboards as code

- **Grafana Cloud free tier**, one stack for the group. No Amazon Managed Grafana, no self-hosted
  Grafana.
- **Data sources:** CloudWatch (metrics and Logs Insights) and X-Ray, in `eu-north-1`. Grafana
  reads through an IAM role, `devops-g9-grafana-read`, that trusts the AWS account and external ID
  Grafana Cloud shows on its data source page. The role is read-only: CloudWatch, Logs Insights,
  X-Ray and tag lookups. No access keys.
- **Dashboards are JSON in `infra/grafana/*.json`**, one per service plus an overview, imported
  into the stack. The repo is the contract; a change made only in the Grafana UI does not count.
- **Every member gets a login** so any of us can open it at a defence.

### 2. Where each signal comes from

| Layer | Source | Owner |
|---|---|---|
| Uptime | A probe Lambda, every minute, calls the public API Gateway `/ready` for POS and Payments and writes `ProbeSuccess` (0 or 1, dimension `target`) to namespace `TillFlow/Probe` | `@emebetgirmay` |
| RED per service, interim | ALB target group `RequestCount`, `HTTPCode_Target_5XX_Count`, `TargetResponseTime` | `@emebetgirmay` |
| Saturation | Container Insights CPU and memory per ECS service | `@emebetgirmay` |
| Payments | `GET /metrics`, scraped by the ADOT sidecar, per ADR 0009 | `@chesangJ` |
| POS | `GET /metrics` on the same scrape path, names below | `@Moraaalice` |
| Commission | CloudWatch Logs metric filters on the JSON summary line `close.py` and `disburse.py` already print; the payout SLI itself comes from Payments' `payments_records` (ADR 0009 section 3) | `@emebetgirmay` (filters), `@chesangJ` (fields) |

**POS metric names** (the Reliability DRI approves any rename or new label):

| Metric | Type | Labels |
|---|---|---|
| `pos_http_requests_total` | counter | `route` (the route template, never the raw path), `status_class` |
| `pos_http_request_duration_seconds` | histogram | `route` |
| `pos_sale_creates_total` | counter | `result` (`created`, `replayed`, `conflict`, `invalid`) |
| `pos_payment_events_total` | counter | `result` (`applied`, `replay`, `rejected`) |
| `pos_sales_paid_total` | counter | none |

**No IDs in labels**, in any service: no tenant, sale, payment, attendant, phone number or trace
id. Those go in logs only.

The ALB rows are interim. Once a service's own SLI metrics exist, its burn alarms move to them and
the ALB rows stay only as a platform view.

### 3. SLIs and burn alarms

Budgets are the SLO doc's 28-day ones. Two burn windows per service:

- **Fast burn:** error rate above 14.4 x (1 - target) over 5 minutes, one period. Pages.
- **Slow burn:** error rate above 6 x (1 - target) over 30 minutes, one period. Slack only.

| Service | Target | Fast burn | Slow burn |
|---|---|---|---|
| POS | 99.9% | 1.44% | 0.6% |
| Payments | 99.5% | 7.2% | 3% |
| Commission | 99.0% | Not a rate: see `payout-not-settled` | |

Rules for every alarm:

- Missing data is **not breaching**, and a zero denominator is not a burn (no traffic is not an
  outage).
- App metrics arrive through EMF with extra dimensions, so alarms use metric math with `SEARCH()`
  rather than an exact dimension match, which would silently never fire.
- `/health` and `/ready` are excluded from SLI math.

**Alarm table.** Names are `devops-g9-<signal>`. The Payments-specific rows are ADR 0009 section 4
and are not repeated here.

| Alarm | Source | Threshold | Owner |
|---|---|---|---|
| `probe-down` | `ProbeSuccess` minimum, per target | < 1 for 2 x 60 s | `@emebetgirmay` |
| `pos-fast-burn`, `pos-slow-burn` | ALB target group `pos`, then `pos_http_requests_total` | Table above | `@Moraaalice` |
| `payments-fast-burn`, `payments-slow-burn` | ALB target group `payments`, then ADR 0009 SLIs | Table above | `@chesangJ` |
| `ecs-cpu-high` | Container Insights CPU, any service | > 70% for 2 x 5 min | `@emebetgirmay` |
| `commission-run-failed` | Metric filter on a non-zero exit or `aborted` in the summary line | >= 1 | `@chesangJ` |
| `payout-not-settled` | Disbursements not `SUCCEEDED` or `FAILED` at 06:30 EAT (`payments_records`) | > 0 | `@chesangJ` |

### 4. Slack path

```
CloudWatch alarm (alarm_actions AND ok_actions)
  -> SNS devops-g9-alerts (encrypted with our own KMS key)
  -> Lambda devops-g9-slack-notifier
       reads secret devops-g9/slack-webhook ({"url": "..."}) on every invoke
  -> Slack: FIRING and RECOVERED
```

- The webhook exists only in Secrets Manager. Terraform creates the secret with a placeholder; a
  member sets the real value out of band. Never in Git, Terraform variables or Lambda environment.
- The SNS topic uses a customer-managed KMS key, because CloudWatch cannot publish to a topic
  encrypted with the AWS-managed SNS key.
- Each alarm's `alarm_description` is JSON carrying the runbook's contract fields: `environment`,
  `service`, `symptom`, `slo_impact`, `observed`, `grafana_panel`, `runbook`, `owner`,
  `first_safe_action`. The Lambda formats them.
- **The drill uses a real symptom** (for example POS scaled to zero, which trips `probe-down`),
  never `SetAlarmState`. Evidence is the alarm history, the SNS publish and the Lambda invocation,
  for both the firing and the recovery.

### 5. k6 runs inside the VPC

- k6 runs as a one-off ECS task, `devops-g9-k6`, in the private subnets, against the internal ALB.
  The image is `grafana/k6` pinned by digest, mirrored into ECR (no `latest`, per the release
  rules).
- This lets `/_fake/*` and `/_admin/*` leave the public route (ADR 0009 G3-7) without breaking
  the harness that needs them.
- Stages: smoke, stepped, spike and a soak of at least 15 minutes, against the fake adapter only.
  Thresholds from the SLO doc: failed under 1%, p95 under 500 ms, checks over 99%, CPU under 70%,
  memory under 75%.
- The k6 end-of-test summary goes to the task log as JSON and is copied into evidence, with an
  analysis: highest sustained rate, p95, bottleneck, CPU and memory.
- **Saturation control:** ECS target tracking on 70% CPU for POS and Payments, which is also the
  `ecs-cpu-high` line.
- SQLite serialises writers, so the numbers prove correctness under concurrency, not capacity
  (ADR 0009 section 6).

### 6. Evidence

`evidence/reliability-ops/g3-evidence.md` is the single index: links to the dashboards, the
Grafana proof (API export of dashboards and data sources plus one screenshot), the Slack drill,
the k6 export and analysis, and a **Not claimed** section listing what G3 does not prove.
Every member signs it.

## Consequences

- One Grafana login works for the whole group without depending on Identity Center.
- Dashboards survive a stack rebuild because they are files, not UI state.
- The CI deploy role gains SNS, Lambda, Secrets Manager, KMS and EventBridge permissions. That is
  a wider blast radius for the deploy role, limited to `devops-g9-*` names where the service
  allows it.
- Uptime, RED and saturation panels work before any service emits a metric; service-level SLIs
  follow as each `/metrics` lands.
- k6 needs its own task definition and image, and cannot be run from a laptop against the public
  edge.
- Commission metrics depend on the shape of one log line; changing it is a breaking change for
  the metric filters.

## Alternatives considered

| Option | Rejected because |
|---|---|
| Amazon Managed Grafana | Login needs Identity Center user assignment, which cohort roles are denied; a stuck workspace can block Terraform later |
| Self-hosted Grafana on ECS | Another service to patch, back up and expose, for no gain over the free tier |
| Amazon Managed Prometheus | ADOT already exports to CloudWatch; a second metrics store adds cost and IAM for nothing G3 needs |
| AWS Chatbot instead of a Lambda | Needs Slack workspace admin approval, and cannot render the runbook's contract fields |
| k6 against the public API Gateway | Needs `/_fake/*` and `/_admin/*` public, which ADR 0009 removes |
| Drill with `SetAlarmState` | Proves Slack formatting, not that the alarm sees a real failure |
| CloudWatch Synthetics canary for uptime | Needs an S3 bucket and Synthetics permissions; a scheduled Lambda gives the same signal |

## Open questions

| # | Question | Owner |
|---|---|---|
| 1 | The web service is still a placeholder. Its SLO is not claimed at G3 unless it ships | `@Moraaalice` |
| 2 | Where the Payments 60 s window starts (ADR 0009 question 2) | `@emebetgirmay` |
| 3 | 28-day panels need 28 days of data; at G3 they show what exists, labelled as such | `@emebetgirmay` |
| 4 | Whether the payout cutoff check runs as an EventBridge-scheduled Lambda or a daily alarm period | `@emebetgirmay` |

## Work items

| ID | Item | Owner | Depends on |
|---|---|---|---|
| R-1 | CI deploy role: SNS, Lambda, Secrets Manager, KMS, EventBridge, Scheduler, EFS (EFS and Scheduler are the G2 Commission follow-up) | `@emebetgirmay` | none |
| R-2 | Slack path: secret placeholder, KMS key, SNS topic, notifier Lambda | `@emebetgirmay` | R-1 |
| R-3 | Probe Lambda, `probe-down`, ALB-based burn alarms, `ecs-cpu-high` | `@emebetgirmay` | R-2 |
| R-4 | `devops-g9-grafana-read` role, dashboards JSON, Grafana Cloud import and proof | `@emebetgirmay` | R-1 |
| R-5 | ADOT Prometheus scrape for Payments and POS (ADR 0009 G3-7) | `@emebetgirmay` | G3-1, P-1 |
| R-6 | Move burn alarms to app SLIs; Payments alarms from ADR 0009 section 4 | `@emebetgirmay` | R-5 |
| R-7 | Commission metric filters, `commission-run-failed`, `payout-not-settled` | `@emebetgirmay` | Commission deployed |
| R-8 | k6 task definition and ECR mirror, run, analysis | `@emebetgirmay` | G3-4 |
| R-9 | Target tracking at 70% CPU for POS and Payments | `@emebetgirmay` | R-1 |
| R-10 | Real-symptom Slack drill and `g3-evidence.md` with sign-offs | `@emebetgirmay` | R-2 to R-8 |
| P-1 | POS `GET /metrics` with the names in section 2 | `@Moraaalice` | none |
