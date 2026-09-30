# ADR 0002 — RDS PostgreSQL baseline

- **Status:** Accepted (2026-09-30, G4/G5)
- **Date:** 2026-09-09
- **DRI:** Platform + delivery — Emebet Girmay (`@emebetgirmay`)
- **Proof:** this ADR + `terraform plan`

## Context

POS, Payments, and Commission need durable relational state with least privilege. RPO ties to backup retention.

## Decision

| Choice | Value |
|---|---|
| Engine | PostgreSQL **16.15**, pinned; `auto_minor_version_upgrade = false`, upgrades by PR |
| Instance | `db.t4g.micro` (sandbox) — revisit if k6 shows CPU pressure |
| Storage | gp3, start **20 GiB**, autoscaling cap **50 GiB**, encrypted (aws/rds) |
| HA | **Single-AZ** for sandbox cost; Multi-AZ if mentor requires prod-like HA |
| Isolation | **One cluster**, **per-service schemas** + least-privilege DB roles (`pos`, `payments`, `commission`) |
| Pooling | App-side pool small; consider RDS Proxy only if connection storms appear |
| Backup | Daily at 20:00–20:30 UTC (23:00 EAT, after trading, before Commission closes the day); retention **7 days**; point-in-time restore → **RPO ≈ 5 minutes** (PITR), 24 h from snapshots alone |

## Implementation

| What | Where |
|---|---|
| Instance `devops-g9-db`, database `tillflow`, subnet group (private subnets), parameter group (`rds.force_ssl = 1`, slow statements over 500 ms logged), security group (5432 from POS, Payments and the bootstrap task only) | `infra/envs/sandbox/rds.tf` |
| Master password created and kept by RDS in Secrets Manager (`manage_master_user_password`): never in Terraform, its state or CI | `rds.tf` |
| One login role and schema per service; each role's `search_path` is its schema; `PUBLIC` loses `CREATE` on `public` and access to the database | Bootstrap SQL in `rds.tf`, run by the one-off `devops-g9-db-bootstrap` task via `infra/scripts/rds-bootstrap.sh`; tested on Postgres 16.15: idempotent, and a role cannot read or drop another schema's tables |
| Per-service secret `devops-g9/db/<service>` (`url`, `sqlalchemy_url`, …), created empty by Terraform, written by a person with the script | `rds.tf`, `infra/scripts/rds-bootstrap.sh` |
| Services switch by variable (`pos_database`, `payments_database` = `sqlite` \| `rds`); the task gets `DATABASE_URL` from its own secret and only its execution role can read it | `rds.tf`, `ecs.tf` |
| Alarms `db-cpu-high`, `db-storage-low`, `db-memory-low` → Slack; runbook "Database" | `rds.tf`, `docs/runbook.md` |
| Final snapshot on destroy (`devops-g9-db-final`), no deletion protection, so destroy and rebuild needs no console step | `rds.tf` |

Not yet: Multi-AZ (sandbox), RDS Proxy (no connection storms seen), Commission's task (after its
Postgres storage lands).

## Consequences

- Service migrations own their schema only
- Platform owns instance, parameter group, subnet group, security groups
- Restore drill (G4) uses a **safe target** instance, then reconcile Daraja refs

## Alternatives

- Separate RDS per service — cost/ops heavy for capstone
- Aurora Serverless — optional later; not required for G1
