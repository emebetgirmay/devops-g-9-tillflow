# ADR 0002 — RDS PostgreSQL baseline

- **Status:** Proposed (G0 — decide before G1)
- **Date:** 2026-09-09
- **DRI:** Platform + delivery — Emebet Girmay (`@emebetgirmay`)
- **Proof:** this ADR + `terraform plan`

## Context

POS, Payments, and Commission need durable relational state with least privilege. RPO ties to backup retention.

## Decision (draft)

| Choice | Value |
|---|---|
| Engine | PostgreSQL **16.x** (pin exact minor in Terraform) |
| Instance | `db.t4g.micro` (sandbox) — revisit if k6 shows CPU pressure |
| Storage | gp3, start **20 GiB**, autoscaling cap documented in TF |
| HA | **Single-AZ** for sandbox cost; Multi-AZ if mentor requires prod-like HA |
| Isolation | **One cluster**, **per-service schemas** + least-privilege DB roles (`pos`, `payments`, `commission`) |
| Pooling | App-side pool small; consider RDS Proxy only if connection storms appear |
| Backup | Automated daily window off-peak; retention **7 days** → RPO ≤ 24h (+ PITR if enabled) |

## Consequences

- Service migrations own their schema only
- Platform owns instance, parameter group, subnet group, security groups
- Restore drill (G4) uses a **safe target** instance, then reconcile Daraja refs

## Alternatives

- Separate RDS per service — cost/ops heavy for capstone
- Aurora Serverless — optional later; not required for G1
