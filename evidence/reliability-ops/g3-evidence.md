# G3 evidence: TillFlow reliability and operations

**From:** Emebet Girmay (`@emebetgirmay`), Reliability + operations and Platform, Group 9
**Date:** 2026-09-29 · **Region:** `eu-north-1` · **Prefix:** `devops-g9-` · **Decisions:** [ADR 0010](../../docs/adrs/0010-reliability-observability.md), [ADR 0009](../../docs/adrs/0009-payments-observability.md)

Payments ran the **FakeAdapter** for every load test and drill below; the Daraja sandbox contract
test (`@chesangJ`) was run separately and switched back before any load. The Slack webhook lives
only in Secrets Manager (`devops-g9/slack-webhook`), never in Git, Terraform or a Lambda variable.

## What G3 asks for, and where it is

| G3 requirement ([SLO doc](../../docs/slo-error-budgets.md), "Next gate") | Evidence |
|---|---|
| Grafana: uptime 5 m / 1 h / 28 d | Overview dashboard, "Edge uptime" stats ([`g3-grafana-overview.png`](g3-grafana-overview.png)) |
| SLO target, budget remaining, burn rate | Overview: per-service availability 5 m / 1 h / 28 d, "Budget left 28d", burn-rate charts with the 6x and 14.4x alarm lines |
| RED and saturation | POS and Payments dashboards: requests, 5xx/4xx, p50/p95, CPU and memory, healthy targets |
| Business signals | Sales paid, payment creates by result, callbacks, rows by state, oldest unresolved age, kill switch, anomalies |
| Slack alerts that fire **and** recover | Three real incidents plus a drill, below; [`drill-checks.json`](drill-checks.json) |
| k6 against the fake adapter | [`k6-analysis.md`](k6-analysis.md): smoke, stepped, spike, 15-minute soak; three runs |

## Grafana

- **Stack:** Grafana Cloud `https://honestmesa567.grafana.net` (ADR 0010: no Amazon Managed Grafana; cohort SSO blocks user assignment).
- **Data source:** CloudWatch in `eu-north-1` through the read-only role `devops-g9-grafana-read`, which only Grafana Cloud's account `008923505280` can assume, and only with this stack's external ID (#41). No access keys.
- **Dashboards as code:** [`infra/grafana/build.py`](../../infra/grafana/build.py) generates `overview.json`, `pos.json`, `payments.json` (uids `tillflow-overview`, `tillflow-pos`, `tillflow-payments`), imported into the stack (#42, #53).
- **Sources:** ALB per target group (RED, availability), Container Insights (saturation), the edge probe (`TillFlow/Probe`), and the services' own `GET /metrics` scraped by the ADOT sidecar into CloudWatch `TillFlow` (#40, with POS #35 and Payments #39). `/metrics` is blocked at the ALB, so it is not public.
- **28-day panels** show the data that exists since G3 started, not 28 full days.

## Alarms and the Slack path

```
CloudWatch alarm (ALARM and OK) -> SNS devops-g9-alerts (own KMS key) -> Lambda devops-g9-slack-notifier -> Slack #devops-g9-alerts
                                                                            reads devops-g9/slack-webhook on every invoke
```

Every alarm's description carries the runbook's alert contract (environment, service, symptom,
SLO impact, observed value, Grafana panel, runbook link, owner, first safe action); the Lambda
renders it (#37). Every alarm links to its own section of [`docs/runbook.md`](../../docs/runbook.md).

| Alarm (`devops-g9-…`) | Signal | PR |
|---|---|---|
| `probe-down` | Public `/ready` through API Gateway, every minute; missing data counts as down | #38 |
| `pos-down`, `payments-down` | No healthy ALB target for 2 minutes | #38 |
| `pos-fast-burn`, `pos-slow-burn` | Target 5xx share over 14.4x (5 min) / 6x (30 min) of the 0.1% budget | #38 |
| `payments-fast-burn`, `payments-slow-burn` | Same, 0.5% budget | #38 |
| `pos-cpu-high`, `payments-cpu-high` | CPU over 70% for 10 minutes | #38 |
| `alb-5xx-fast-burn`, `alb-5xx-slow-burn` | 5xx the ALB generates itself (dropped connections), invisible to the target 5xx counts | #53 |
| `payments-critical-anomaly`, `-payouts-paused`, `-needs-review`, `-payment-unknown-too-long`, `-payout-unknown-too-long`, `-reconcile-stale` | Payments' own metrics via Metrics Insights queries (ADR 0009 section 4) | #43, #44 |

**17 alarms**, all created by Terraform through the gated pipeline.

## Edge probe history

`devops-g9-probe` calls the public `/ready` (API Gateway → VPC link → ALB → POS) once a minute.
Exported with [`collect-probe.sh`](collect-probe.sh) on 2026-09-30, before CloudWatch rolls the
one-minute data up after 15 days: [`probe-summary.json`](probe-summary.json),
[`probe-minutes.json`](probe-minutes.json) (every minute), [`probe-alarm-history.json`](probe-alarm-history.json).
A failed **or missing** minute counts as down, as `probe-down` treats it.

| Window (UTC) | Minutes | Up | Failed | Missing | Availability | Latency p50 / p95 / max |
|---|---|---|---|---|---|---|
| 2026-09-29 13:00 → 2026-09-30 11:04 | 1,325 | 1,325 | 0 | 0 | **100.0%** | 55 / 77 / 469 ms |

`probe-down`'s only state change was at creation: ALARM at 13:00:33, OK at 13:01:33, before the
probe's first datapoint (missing data counts as down, by design). After that, no down minute in a
window that includes the k6 runs, the Slack drill, the POS release that was rolled back automatically (#54) and the Payments resize (#55):
none of them was visible at the public edge. One day of history is not an SLO measurement over
28 days; it shows the probe runs every minute without gaps and what it measures.

## Incidents: real symptoms, never `SetAlarmState`

| # | When (UTC) | What fired | Cause | Resolution | Evidence |
|---|---|---|---|---|---|
| 1 | 15:44 → 16:42 | `payments-reconcile-stale` | **A real production gap:** nothing ran Payments' reconcile pass on a schedule, so `UNKNOWN` payments and payouts would never resolve | Built G3-10: an in-VPC Lambda calls `POST /_admin/sweep` every 5 minutes (#45). Recovered 16:42; the RECOVERED post reached Slack. The FIRING post was dropped because the webhook was not yet set (the notifier logs `slack_webhook_not_set`, by design). | alarm history; notifier log |
| 2 | 16:48 → 17:13 | `payments-reconcile-stale` (**drill**) | The sweep schedule was **disabled through a PR** (#46): a real component stopped | Re-enabled through a PR (#48). FIRING 17:02, RECOVERED 17:13, both posted with status 200 | [`g3-drill-2-firing.png`](g3-drill-2-firing.png), [`g3-drill-3-recovered.png`](g3-drill-3-recovered.png), [`drill-checks.json`](drill-checks.json) (6/6 checks), [`collect-drill.sh`](collect-drill.sh) |
| 3 | 17:12 → 17:16 | `payments-critical-anomaly` (P0 page) | `@chesangJ`'s G4 replay/reorder drill (#47) sent contradicting results for already-terminal payments and a payout. Payments logged each as `illegal_transition` and **did not apply it** | No action needed: the invariant held. The alarm paged within about a minute with the right owner and first safe action, and recovered when the drill ended | [`g3-critical-anomaly-g4-drill.png`](g3-critical-anomaly-g4-drill.png); Payments log |
| 4 | 21:11 → 21:18 | `payments-cpu-high` | k6 soak at 0.25 vCPU: Payments CPU at 75% average, 99.75% peak | Payments to 0.5 vCPU (#55); the final k6 run peaked at 55% with no alarm | [`g3-cpu-high-k6.png`](g3-cpu-high-k6.png), [`k6-analysis.md`](k6-analysis.md) |

Drill timeline from the collected data: last sweep 16:45:46, schedule disabled about 16:50, no
sweeps until 17:12:42, alarm OK → ALARM 17:02:05, schedule re-enabled about 17:10, alarm
ALARM → OK 17:13:05. A Payments deploy (#47) overlapped 17:06–17:10; it did not affect the result.

## Trace: sale to payment to callback in X-Ray

POS and Payments export spans to their ADOT sidecar, which sends them to X-Ray (#71 trace ids,
#74 spans, `@chesangJ`). Captured on the deployed sandbox with
[`xray/collect-trace.sh`](xray/collect-trace.sh): one sale through the public URL, the fake
provider's callback, the reconcile. Trace `1-6abd2d70-f86eda7826fe5e01d6ca13a7`
([`xray/trace.json`](xray/trace.json), [`xray/checks.json`](xray/checks.json): 4/4):

```
   offset  duration  service   span
      0ms      30ms  pos       POST /tenants  [201]
   3337ms      29ms  pos       POST /tenants/{tenant_id}/sales  [201]
   4056ms     177ms  pos       POST /tenants/{tenant_id}/sales/{sale_id}/payment-request  [200]
   4205ms      17ms  payments  POST /payments  [201]
   5582ms       1ms  payments    payment PENDING to SUCCEEDED        <- the provider's callback
   6253ms      31ms  pos       POST /tenants/{tenant_id}/sales/{sale_id}/payment-reconcile  [200]
   6268ms       1ms  payments  GET /payments/{id}  [200]
```

(Tills, attendants and products omitted here; all in [`xray/waterfall.txt`](xray/waterfall.txt).)
Payments' span is a child of POS's request span; the callback arrives as its own request with no
trace header of ours and is still placed in the sale's trace, as a child of the payment's create
span, because Payments stores the creating span with the record. Screenshot: `xray/waterfall.png`.

## k6

Full write-up: [`k6-analysis.md`](k6-analysis.md). In short:

| | Before fix | After fix, 0.25 vCPU | **Final, 0.5 vCPU** |
|---|---|---|---|
| Failed | 0.31% (110 ALB 502s) | 0.00% | **0.00%** |
| p95 | 229 ms | 546 ms | **154 ms** |
| Checks | 100% | 100% | **100%** |
| Payments CPU peak | 85% | 99.75% | **55%** |

Money invariants held after every run (final: 27,988 succeeded payments = 27,988 credits, 0 duplicates).

The load test found and we fixed: a thread-safety bug that produced silent 502s (#52), silent
failures reaching no signal (#52), a monitoring blind spot for ALB-generated 5xx (#53) and CPU
saturation (#55). Along the way the pipeline caught a FastAPI release that crashed POS at start-up
and **rolled it back automatically** (fixed in #54, with a new CI start-up check), and PR checks
caught a Commission test that failed every evening from 21:00 UTC (#56).

## Not claimed

- **Horizontal autoscaling.** Each task keeps its own SQLite database, so a second task would split state and break correctness. Saturation is observed and alarmed (CPU panels, `*-cpu-high`) and was fixed by vertical sizing (#55). Autoscaling follows the Postgres/RDS backend (ADR 0002).
- **Production capacity numbers.** SQLite serialises writers (ADR 0009 section 6); the k6 figures prove correctness under concurrency and headroom on this build, not production sizing.
- **Two ADR 0009 alarms:** callback rejections above baseline, and the provider-unknown rate above 5%. Both need a baseline from real traffic, and the second a ratio a single Metrics Insights alarm cannot express.
- **28 days of history.** 28-day panels cover the data since G3 started.
- **The web service.** Still a placeholder; its SLO is not measured (ADR 0010 open question 1).
- **Real Daraja under load.** Load is FakeAdapter only, by design.

## Reproduce

```bash
aws sso login --profile g9
./evidence/reliability-ops/run-k6.sh                                           # k6 envelope, about 17 minutes
DRILL_FROM=2026-09-29T16:45:00Z DRILL_TO=2026-09-29T17:14:08Z ./evidence/reliability-ops/collect-drill.sh
./evidence/reliability-ops/collect-probe.sh                                     # probe history, last 15 days
python3 infra/grafana/build.py                                                  # regenerate the dashboards
```

## Sign-off

| Name | Role | Signed |
|---|---|---|
| Emebet Girmay (`@emebetgirmay`) | Platform + delivery; Reliability + operations | 2026-09-30 |
| Mitingi Joy Chesang (`@chesangJ`) | Payments + integrity | 2026-09-30 |
| Alice Moraa (`@Moraaalice`) | Product + POS | 2026-09-30 |
