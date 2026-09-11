# devops-g9 — TillFlow

Multi-tenant POS + M-Pesa (Daraja **sandbox**) on AWS ECS.

Private group mono-repo · Terraform · GitHub Actions + CodePipeline.

**Flow:** attendant records a sale → customer pays via STK Push → commission worker pays attendants via B2C (through Payments).

| | |
|---|---|
| **Due** | Mon 21 Sep 2026, 23:59 EAT |
| **Prefix** | `devops-g9` |
| **Region** | `eu-north-1` ([ADR](docs/adrs/0001-aws-region.md)) |
| **Focus** | G1 Platform — Terraform, ECS golden path, first pipeline |

## Repository layout

```
/
├─ services/
│  ├─ web/           # tenant / attendant UI
│  ├─ pos/           # sales & tenant API
│  ├─ payments/      # Daraja STK / B2C / callbacks
│  ├─ commission/    # daily payout worker
│  └─ _shared/       # adapters, OTel, Docker base
├─ infra/            # Terraform
├─ .github/workflows/# PR checks + gated apply
├─ docs/             # architecture, ADRs, SLOs, runbook, …
├─ evidence/<area>/  # runtime proof per DRI
└─ CODEOWNERS
```

## Documentation

| Doc | Purpose |
|---|---|
| [Architecture](docs/architecture.md) | System design and service boundaries |
| [Ownership](docs/ownership.md) | DRIs, cross-review map, path ownership |
| [ADRs](docs/adrs/) | Region, RDS, S3, M-Pesa adapter |
| [Threat model](docs/threat-model.md) | Security assumptions and mitigations |
| [SLOs & error budgets](docs/slo-error-budgets.md) | Reliability targets |
| [Runbook](docs/runbook.md) | Operate / recover procedures |
| [Production readiness](docs/production-readiness.md) | Release checklist (fills through G5) |
| [Scar log](docs/scar-log.md) | Incidents and lessons |

Per-service and infra notes live next to the code. Gate proof goes under `evidence/` (`product-pos`, `payments-integrity`, `platform-delivery`, `reliability-ops`).

## Ownership

| Area | DRI |
|---|---|
| Product + POS | Alice Moraa (`@Moraaalice`) |
| Payments + integrity | Mitingi Joy Chesang (`@chesangJ`) |
| Platform + delivery | Emebet Girmay (`@emebetgirmay`) |
| Reliability + operations | Emebet Girmay *(combined — see [ownership.md](docs/ownership.md))* |

PRs follow `CODEOWNERS` and the cross-review map in ownership.md.

## Delivery gates

| Gate | Due | Focus |
|---|---|---|
| G0 Decide | D2 (9 Sep) | Ownership, architecture, ADRs, threat model, draft SLOs |
| G1 Platform | D5 (12 Sep) | Terraform apply, ECS golden path, first pipeline |
| G2 Product | D8 (15 Sep) | Sale → STK → paid; commission → B2C |
| G3 Operate | D11 (18 Sep) | Grafana, k6, Slack alerts |
| G4 Recover | D13 (20 Sep) | Failure drills + restore |
| G5 Release | D14 (21 Sep) | Fresh release + defences |

## Conventions

- Deploy **only** in `eu-north-1`; name resources `devops-g9-…`
- Required tags: `group`, `owner`, `environment`, `service`, `managed-by=terraform`, `capstone=tillflow`
- Daraja **sandbox only** — never commit live credentials or customer data
- CI / k6 use the deterministic fake M-Pesa adapter
- Bootstrap (`terraform init/plan`, service targets, destroy) lands in G1 under `infra/` — Platform DRI owns destroy/rebuild evidence; cost tracked at G5
