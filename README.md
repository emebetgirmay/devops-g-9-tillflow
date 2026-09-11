# devops-g9 — TillFlow

Multi-tenant POS + M-Pesa (Daraja **sandbox**) on AWS ECS.

Private group mono-repo · Terraform · GitHub Actions + CodePipeline.

| | |
|---|---|
| **Due** | Mon 21 Sep 2026, 23:59 EAT |
| **Prefix** | `devops-g9` |
| **Region** | `eu-north-1` ([ADR](docs/adrs/0001-aws-region.md)) |

## Current gate

**G0 — Decide** (D2 · 9 Sep 2026)

| Evidence | Status |
|---|---|
| Private mono-repo scaffold | done |
| Teammates invited (Write) | done |
| Mentor access invited | open |
| Ownership matrix with real names | done — [`docs/ownership.md`](docs/ownership.md) |
| Architecture | done — [`docs/architecture.md`](docs/architecture.md) |
| ADRs (incl. region) | done — [`docs/adrs/`](docs/adrs/) |
| Threat model | done — [`docs/threat-model.md`](docs/threat-model.md) |
| Draft SLOs | done — [`docs/slo-error-budgets.md`](docs/slo-error-budgets.md) |
| `CODEOWNERS` handles updated | done |

Blocked if any member has no primary area, or a critical decision has no DRI.

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
├─ docs/             # ownership, architecture, ADRs, SLOs, …
├─ evidence/<area>/  # runtime proof per DRI
└─ CODEOWNERS
```

## Documentation

| Doc | Purpose |
|---|---|
| [Architecture](docs/architecture.md) | System design and service boundaries |
| [Ownership](docs/ownership.md) | DRIs, cross-review map, path ownership |
| [ADRs](docs/adrs/) | Region, RDS, S3, M-Pesa adapter decisions |
| [Threat model](docs/threat-model.md) | Security assumptions and mitigations |
| [SLOs & error budgets](docs/slo-error-budgets.md) | Reliability targets |
| [Runbook](docs/runbook.md) | Operate / recover procedures |
| [Production readiness](docs/production-readiness.md) | Release checklist |
| [Scar log](docs/scar-log.md) | Incidents and lessons |

Service and infra notes live next to the code (`services/*/README.md`, `infra/README.md`).

## Ownership

| Area | DRI |
|---|---|
| Product + POS | Alice Moraa (`@Moraaalice`) |
| Payments + integrity | Mitingi Joy Chesang (`@chesangJ`) |
| Platform + delivery | Emebet Girmay (`@emebetgirmay`) |
| Reliability + operations | Emebet Girmay *(combined — see [ownership.md](docs/ownership.md))* |

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

- Deploy **only** in the assigned region; name resources `devops-g9-…`
- Tag: `group`, `owner`, `environment`, `service`, `managed-by=terraform`, `capstone=tillflow`
- Daraja **sandbox only** — never commit live credentials or customer data
- CI / k6 use the deterministic fake M-Pesa adapter

## Bootstrap

```bash
# TBD G1: terraform init/plan, service make targets, destroy
```

Platform DRI owns destroy / rebuild evidence. Cost tracking lands at G5.
