# Cost model — TillFlow sandbox

**Owner:** Platform, Emebet Girmay (`@emebetgirmay`) · **Measured:** 2026-09-30, bill window 2026-09-16 → 2026-09-29 ·
**Region:** `eu-north-1` · **Evidence:** [`evidence/platform-delivery/cost/`](../evidence/platform-delivery/cost/)

**TillFlow costs about $151 a month (≈ $5.00 a day) at today's size**, RDS and Commission
included (updated 2026-09-30, below). Almost all of it is fixed: the NAT gateway, the load balancer,
CloudWatch custom metrics, the database and two small Fargate services. Traffic adds cents. A lab
stack left running in the same region costs more than TillFlow (below).

## How it is measured

The AWS account is shared by the cohort: other teams run in other regions, and `eu-north-1` holds
one other stack. The `group` tag is not active for cost allocation, so Cost Explorer cannot split
the bill by team. [`collect-cost.sh`](../evidence/platform-delivery/cost/collect-cost.sh) therefore:

1. reads the `eu-north-1` bill per usage type from Cost Explorer (usage only, no credits), which
   gives the **unit price actually paid** for each item;
2. builds TillFlow's cost **bottom-up** from its own resources (tag `group=devops-g9`) and its own
   CloudWatch usage figures (log bytes per log group, bytes through its NAT, ALB LCUs, API and
   Lambda calls), times those unit prices;
3. costs the other stack in the region the same way and **reconciles** both with the measured bill.

A month is 730 hours. Prices marked "list" are AWS list prices for items the window's bill does
not show (free tier, or too small to price).

## TillFlow, per month

| Area | Component | Basis | Unit price (USD) | Monthly (USD) |
|---|---|---|---|---|
| Network | NAT gateway | 1 × 730 h | 0.046 / h | 33.58 |
| Network | Load balancer (internal ALB) | 1 × 730 h | 0.02394 / h | 17.48 |
| Network | Public IPv4 (NAT's Elastic IP) | 1 × 730 h | 0.005 / h | 3.65 |
| Network | NAT data processed | 9.8 GB in 14 days | 0.046 / GB | 0.98 |
| Network | ALB LCUs | 1.2 LCU-h in 14 days | 0.0076 / LCU-h | 0.02 |
| Compute | Fargate vCPU: POS 0.25 + Payments 0.5 (ADOT sidecars inside) | 0.75 × 730 vCPU-h | 0.0445 / vCPU-h | 24.36 |
| Compute | Fargate memory: POS 0.5 + Payments 1 GB | 1.5 × 730 GB-h | 0.0049 / GB-h | 5.37 |
| Observability | Custom metrics: ADOT scrape 86, Container Insights 63, probe 2 | 151 metrics | 0.30 / metric | 45.30 |
| Observability | Alarms: 11 standard, 6 Metrics Insights | 17 alarms | 0.10 / alarm | 1.70 |
| Observability | Logs ingestion (mostly Payments during k6) | 0.13 GB in 14 days | 0.54 / GB | 0.15 |
| Observability | Logs storage (14-day retention) | 0.05 GB | 0.03 / GB (list) | 0.00 |
| Security | Secrets Manager: Daraja, Slack webhook | 2 secrets | 0.40 | 0.80 |
| Security | KMS key for the alerts topic | 1 key | 1.00 (list) | 1.00 |
| Delivery | ECR images (lifecycle policy expires all but the newest 10 per rule) | 1.4 GB | 0.10 / GB | 0.14 |
| Edge | API Gateway, Lambda (probe, sweep, notifier), Scheduler, SNS | ~45,000 probe calls a month plus traffic | list; free tier today | < 0.10 |
| | **Total** | | | **≈ 134.53** |

By area: **network $55.7 (41%)**, **observability $47.2 (35%)**, **compute $29.7 (22%)**, security
and delivery $1.9.

## Reconciliation with the bill

| | USD / month |
|---|---|
| TillFlow, today's size (above) | 134.53 |
| Other stack in the region (below) | 193.62 |
| Sum | 328.15 |
| Measured `eu-north-1` bill, 2026-09-16 → 29, as a monthly rate | 282.11 |

The model is $46 a month above the bill because it prices **today's** configuration, while most of
the window ran a smaller one: the ADOT scrape of `/metrics` (#40, 86 metrics) and the Payments resize
to 0.5 vCPU (#55) both arrived on 2026-09-29. The window's bill averaged about 52 custom metrics
against 151 now, about $30 of the gap; the resize adds about $10. The daily bill shows the step: $8.7–9.5 a day until
2026-09-28, $10.0 on 2026-09-29. Re-run the script after a full month to replace the estimate with
a measured month.

## Other stack in the region: the IaC lab

`devops-g9-iac-*` (tags `environment=lab`, `build=iac`, owner Mitingi Joy, created 2026-08-27) is
an earlier lab exercise, not part of TillFlow, and is still running:

| Component | Basis | Monthly (USD) |
|---|---|---|
| Interface VPC endpoints: ECR API/DKR, logs, SSM, SSM messages, EC2 messages | 6 endpoints × 2 AZ × 730 h at 0.0105 | 91.98 |
| Fargate: service-a × 2, service-b, service-c | 1 vCPU, 2 GB | 39.63 |
| NAT gateway `devops-g9-iac-nat` | 730 h | 33.58 |
| Internet-facing ALB `devops-g9-iac-alb` | 730 h | 17.48 |
| Public IPv4: NAT and ALB | 3 × 730 h | 10.95 |
| **Total** | | **≈ 193.62** |

**Decision for the lab's owner:** destroy it with its own Terraform if it is no longer needed. That
saves more than TillFlow costs to run. It is left untouched here because it is not TillFlow's.

## What drives cost, and the levers

| Lever | Saves / month | Trade-off | Decision |
|---|---|---|---|
| Destroy the IaC lab | ~194 | None if the lab is finished | Owner to decide |
| Turn off Container Insights; use the free `AWS/ECS` CPU and memory metrics | ~19 | Loses per-task and network saturation panels; the CPU alarms already use `AWS/ECS` | Keep for G3–G4 evidence; revisit at G5 |
| Filter the ADOT scrape to the series the dashboards and alarms use | up to ~15 | A new panel needs a config change first | Worth doing when the metric set settles |
| Replace the NAT gateway with VPC endpoints | none: it costs more | Interface endpoints are per AZ; the lab shows 6 × 2 AZ ≈ $92 against NAT's ~$34 | Keep the NAT gateway |
| Stop the sandbox outside working hours (scale ECS to 0) | ~20 on compute | Probe and burn alarms page every night; the fixed network cost stays | Not worth it |

**Traffic is not the driver.** At the k6 rate (36.7 requests/s, all through API Gateway) a month
would be about 96 million requests: about $96 of API Gateway requests, a little ALB LCU and log
ingestion, with Payments already sized for it (CPU peak 55%, [k6 analysis](../evidence/reliability-ops/k6-analysis.md)).
Real shop traffic is orders of magnitude lower.

## Added since: RDS, Commission, X-Ray (2026-09-30)

Priced from the bill again: Cost Explorer already shows the first hours of `devops-g9-db`
(`eu-north-1`, usage only; the AWS Pricing API is denied to the cohort role).

| Component | Basis | Unit price (USD) | Monthly (USD) |
|---|---|---|---|
| RDS `db.t4g.micro`, PostgreSQL, single-AZ | 730 h | 0.016 / h (bill: 0.004911 for 0.307 h) | 11.68 |
| RDS gp3 storage | 20 GB | 0.12 / GB-month (bill) | 2.40 |
| RDS backups (7 days, point-in-time) | up to the database size | free up to 100% of storage | 0.00 |
| Performance Insights | 7-day retention | free tier | 0.00 |
| Secrets Manager: three service logins and the RDS-managed master | 4 secrets | 0.40 | 1.60 |
| Commission: scheduled tasks, 0.25 vCPU / 0.5 GB | about 38 runs a day of about a minute (Fargate bills at least 1 min) ≈ 19 task-hours a month | 0.0445 / vCPU-h, 0.0049 / GB-h | 0.26 |
| CloudWatch: 3 database alarms, Commission alarm and metric | 4 alarms, 1 metric | 0.10, 0.30 | 0.70 |
| X-Ray traces | sandbox traffic, well inside the 100,000 free traces a month | free tier | 0.00 |
| **Added** | | | **≈ 16.64** |

**New total ≈ $151 a month** ($134.53 + $16.64). The database is the fifth-largest line, after the custom
metrics, the NAT gateway, Fargate vCPU and the load balancer. At this size Multi-AZ would double the instance line (about
$12 more) and cut the restore RTO measured in the G4 drill (18 min 44 s) to a failover of a minute
or two; recorded as a decision for production, not the sandbox (ADR 0002).

## Not in this model yet

- **Queue** (SQS with a DLQ for callbacks, when it lands): per request, cents at this volume.
- **Grafana Cloud** is on its free tier; its CloudWatch reads show in the bill as `GetMetricData`
  (under $0.10 in the window).
- **Budgets and anomaly alerts** need the payer account in a shared cohort account; the per-day
  numbers in `cost-daily.json` are the check until then.

## Reproduce

```bash
aws sso login --profile g9
./evidence/platform-delivery/cost/collect-cost.sh     # last 14 full days; COST_FROM / COST_TO to choose
```
