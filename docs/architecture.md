# Architecture — TillFlow (devops-g9)

**Gate:** G0 · **Status:** draft for Decide · **Region ADR:** `docs/adrs/0001-aws-region.md`

## Context

Multi-tenant POS SaaS: attendants record sales, customers pay via M-Pesa STK Push (Daraja 3.0 **sandbox**), and a daily commission worker pays attendants via B2C through the Payments API. Timeouts, duplicate callbacks, and infra failures must remain correct and explainable.

## Logical view

```
WEB (ECS) → API Gateway → VPC Link → ALB → SERVICES (ECS Fargate, private subnets, 2 AZs)
                                              ├─ POS
                                              ├─ Payments  ←── Daraja sandbox (STK, query, B2C)
                                              └─ Commission worker (EventBridge schedule)
                                                    │
                                    STATE + EDGES: RDS | Redis/Valkey | S3 | SQS+DLQ
```

Every backend task = **app container + ADOT Collector sidecar** (OTLP → CloudWatch/Prometheus, X-Ray → Grafana).

## Services

| Service | Responsibility | Does **not** |
|---|---|---|
| **web** | Tenant/attendant UI shell, API client | Own money state |
| **pos** | Tenants, attendants, sales (minor units), idempotent create | Call Daraja |
| **payments** | Daraja auth, STK, callbacks, query, B2C, reconciliation | Invent sale totals |
| **commission** | Daily close from **confirmed paid** sales → payout ledger → B2C **via Payments API** | Call Daraja directly |
| **_shared** | M-Pesa adapter interface, fake adapter for CI/k6, OTel helpers, Docker base | Business workflows |

## Money / state invariants (product contract)

1. Sale amounts are **integer minor units**; create is **idempotent**.
2. A Daraja **timeout is not a decline** — stay pending, query/reconcile.
3. Callback **replay/reorder** → one legal transition, one ledger effect.
4. Commission calculates only from **confirmed paid** sales; **replay never double-pays**.
5. CI and k6 use a **deterministic fake adapter**; sandbox only for small contract tests. Never real money or customer PII in git.

## Platform baseline (owned by Platform DRI)

- API Gateway → VPC Link → ALB; ECS Fargate in private subnets across **two AZs**
- RDS PostgreSQL: per-service schemas + least-privilege roles; connection pooling
- Redis/Valkey cache-aside; SQS + DLQ; EventBridge daily schedule for commission
- S3: tfstate (+ DynamoDB lock), artifacts, ALB logs, backups, evidence
- Terraform owns all infra, pipelines, secret **references**, alarms, dashboards
- Naming: `devops-g9-…`; tags: `group`, `owner`, `environment`, `service`, `managed-by=terraform`, `capstone=tillflow`

## Delivery lanes

| Lane | Path | Gate evidence |
|---|---|---|
| GitHub Actions | PR lint/tests/scans; `plan` on PR; approved `apply` on `main` via OIDC | Checks + plan/apply parity |
| CodePipeline | Source → CodeBuild → scan → ECR (SHA/digest) → ECS → smoke/rollback | Deploy + rollback proof |

Path filters: change under `services/payments/` builds/deploys Payments (+ shared deps) only.

## Trust boundaries (see threat model)

- Public: API Gateway / web edge
- Private: ALB, ECS tasks, RDS, Redis, SQS
- External: Daraja sandbox (Payments only)
- Secrets: Secrets Manager (`devops-g9/daraja`, `devops-g9/slack-webhook`, `devops-g9/db`) — never in git/state/logs

## Open decisions for G0 → G1

Recorded as ADRs before Platform apply:

- AWS region (ADR 0001)
- RDS engine/class/Multi-AZ/backup vs RPO (ADR 0002)
- S3 bucket purposes + lock table (ADR 0003)
- M-Pesa adapter + fake for CI (ADR 0004)
