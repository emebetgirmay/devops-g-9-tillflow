# Production readiness checklist

**Owner:** Platform + delivery, Emebet Girmay (`@emebetgirmay`) · **Updated:** 2026-09-30 (G3; cost model) · Filled through G5.

✅ done with evidence · 🟡 partly · ❌ not done (with the reason and the plan)

## Build and release

| | Item | Evidence |
|---|---|---|
| ✅ | Naming `devops-g9-*` and required tags on every resource | `evidence/platform-delivery/tag-audit.txt`: 42 resources, all required tags present (G1 audit; re-run at G5 for the G3 resources) |
| ✅ | No `latest` tags: SHA build, push by immutable digest, deploy by digest | `.github/workflows/release.yml`; ECR repositories are `IMMUTABLE` (`infra/envs/sandbox/ecr.tf`) |
| ✅ | Secret scan, IaC scan, image scan; HIGH/CRITICAL fails the build | gitleaks, Trivy config and Trivy image in `pr.yml` and `release.yml` (`--severity HIGH,CRITICAL --exit-code 1`); accepted risks listed with owner and expiry in `.trivyignore` |
| ✅ | SBOM per image | `release.yml` "SBOM (CycloneDX)": the pinned Trivy writes a CycloneDX SBOM of each released image; kept 90 days as the `sbom-pos-<sha>` / `sbom-payments-<sha>` artifacts of the Release run |
| ✅ | Gated apply of the reviewed plan | `release.yml`: plan artifact, `sandbox` environment approval, apply of that exact plan |
| ✅ | Post-deploy smoke and automatic rollback | `release.yml` "Wait stable + mandatory smoke" then "Rollback previous task definition". **Exercised for real on 2026-09-29:** a POS build crashed at start-up and was rolled back to revision 24 with no outage ([scar log](scar-log.md)) |
| ✅ | Start-up check in CI with the production environment | `pr.yml` "Start-up smoke (ECS environment)" for POS (#54) |

## Runtime

| | Item | Evidence |
|---|---|---|
| ✅ | Containers run as a non-root user | `USER 10001:10001` in the POS, Payments and Commission Dockerfiles |
| 🟡 | Read-only root filesystem | ADOT sidecars and the k6 task: yes. POS and Payments: no, because SQLite needs a writable path in the image. Becomes yes with RDS |
| ✅ | `/health`, `/ready`, `/version` on every service; ALB and ECS health checks use `/ready` | `infra/envs/sandbox/ecs.tf`, `alb.tf`; live at the public edge |
| ✅ | No public access to metrics or data paths | `/metrics` blocked at the ALB (#40); ALB internal, reachable only through API Gateway's VPC link and the VPC |
| ✅ | Test and operator endpoints not served to the internet on real builds | API Gateway stamps every request with `x-tillflow-edge`; Payments refuses `/_admin/*` and `/_fake/*` carrying it unless it runs the FakeAdapter (`services/payments/tests/test_public_edge.py`). The sandbox runs the FakeAdapter, so the team's drills and demo still use them through the public URL; in-VPC callers (the sweep, k6) are unaffected |
| ✅ | Least-privilege IAM, no long-lived keys | GitHub OIDC role; CI role scoped by name and `group` tag (#33); service roles read only their own secrets; Grafana reads through an external-ID role |
| ✅ | Secrets only in Secrets Manager; CI can describe but not read them | Daraja and Slack secrets; runbook "Slack webhook" |

## Data

| | Item | Evidence |
|---|---|---|
| ❌ | **Managed database with backups** | **POS and Payments use SQLite inside the container: a task restart loses their data.** Plan: RDS PostgreSQL (ADR 0002). This blocks the restore drill and horizontal scaling |
| ❌ | Cache and queue with DLQ | Not built. Design question resolved 2026-09-30 (ADR 0009 question 5): agreed candidate is Daraja callbacks on SQS + a DLQ; POS has no equivalent inbound event to queue (its settlement path is a poll with its own safety net, not a delivery). Callbacks are handled synchronously today, with reconcile as the interim safety net |
| ❌ | Backups, restore drill, measured RTO/RPO | Blocked on RDS (G4) |

## Operate

| | Item | Evidence |
|---|---|---|
| ✅ | SLOs and error budgets, SLIs wired in Grafana | [`slo-error-budgets.md`](slo-error-budgets.md); Grafana overview, POS and Payments dashboards |
| ✅ | Burn-rate alerts (fast and slow) that page and recover in Slack | 17 alarms; [G3 evidence](../evidence/reliability-ops/g3-evidence.md) |
| ✅ | Uptime probe on the public edge | `devops-g9-probe`, every minute, `probe-down`; history exported: 1,325 of 1,325 minutes up ([G3 evidence](../evidence/reliability-ops/g3-evidence.md#edge-probe-history)) |
| ✅ | Load tested, capacity recorded | [k6 analysis](../evidence/reliability-ops/k6-analysis.md): 36.7 req/s sustained, p95 154 ms, 0.00% failed |
| 🟡 | Runbook rehearsed | Slack drill and real incidents followed the runbook ([G3 evidence](../evidence/reliability-ops/g3-evidence.md)); restore not rehearsed (no database) |
| ✅ | Incidents and lessons recorded | [`scar-log.md`](scar-log.md) |
| ❌ | Destroy and rebuild documented and proven | Not done (G5). Needs `force_destroy` / `force_delete` where appropriate and a timed rebuild |
| ✅ | Cost model | [`cost-model.md`](cost-model.md): about $135 a month at today's size, built from TillFlow's own resources and the unit prices in the bill, reconciled with the measured `eu-north-1` bill; levers and decisions listed. Found a lab stack costing ~$194 a month still running |
