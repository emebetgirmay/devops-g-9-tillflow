# devops-g9 — TillFlow

Multi-tenant POS + M-Pesa (Daraja **sandbox**) on AWS ECS.

Private group mono-repo · Terraform · GitHub Actions + CodePipeline.

**Flow:** attendant records a sale → customer pays via STK Push → commission worker pays attendants via B2C (through Payments).

| | |
|---|---|
| **Due** | Mon 21 Sep 2026, 23:59 EAT |
| **Prefix** | `devops-g9` |
| **Region** | `eu-north-1` ([ADR](docs/adrs/0001-aws-region.md)) |
| **Status** | G0–G3 evidenced (see below); G4–G5 in progress |

## Live system

| | |
|---|---|
| **Public API** (API Gateway) | https://ewi66kqbp8.execute-api.eu-north-1.amazonaws.com — try [`/health`](https://ewi66kqbp8.execute-api.eu-north-1.amazonaws.com/health), `/ready`, `/version` |
| **Grafana** | https://honestmesa567.grafana.net — dashboards *TillFlow overview*, *TillFlow POS*, *TillFlow Payments* (all three members have logins) |
| **Alerts** | Slack `#devops-g9-alerts` (firing and recovered, with runbook links) |
| **Payments adapter** | FakeAdapter by default; the Daraja sandbox only for the B2C contract test |

## Evidence by gate

| Gate | Start here |
|---|---|
| G1 Platform | [`evidence/platform-delivery/`](evidence/platform-delivery/README.md) |
| G2 Product | [`evidence/product-pos/`](evidence/product-pos/README.md), [`evidence/payments-integrity/`](evidence/payments-integrity/README.md), [`evidence/commission-payout/`](evidence/commission-payout/README.md), [`evidence/daraja-b2c-contract/`](evidence/daraja-b2c-contract/README.md) |
| G3 Operate | [`evidence/reliability-ops/g3-evidence.md`](evidence/reliability-ops/g3-evidence.md), [k6 analysis](evidence/reliability-ops/k6-analysis.md) |
| G4 Recover | [Broken release and automatic rollback](evidence/platform-delivery/g4-broken-release/README.md); [`evidence/payments-integrity/g4/`](evidence/payments-integrity/g4/) (uncertain payment, uncertain payout, callback replay); restore pending RDS |
| Incidents | [`docs/scar-log.md`](docs/scar-log.md) |

## Known limitations (stated, not hidden)

- **Data lives in SQLite inside each container.** A task restart loses POS and Payments data, the payouts kill switch included; there is nothing to restore and no horizontal scaling. The fix is RDS PostgreSQL ([ADR 0002](docs/adrs/0002-rds-postgresql.md)).
- **No cache or queue yet.** Callbacks are handled synchronously, with a scheduled reconcile pass as the safety net.
- **Commission is tested end to end but not deployed** as a scheduled task.
- `services/web` is a placeholder; there is no web UI.

Commit authorship is mapped in [`.mailmap`](.mailmap) (`git shortlog -sne`): commits from a workstation whose git user was "Administrator" or "root" are Emebet's.

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
| [ADRs](docs/adrs/) | 0001–0010: region, data, delivery lanes, M-Pesa adapter, idempotency, tenancy, payouts, observability |
| [Threat model](docs/threat-model.md) | Security assumptions and mitigations |
| [SLOs & error budgets](docs/slo-error-budgets.md) | Reliability targets |
| [Runbook](docs/runbook.md) | Operate / recover procedures |
| [Production readiness](docs/production-readiness.md) | Release checklist (fills through G5) |
| [Cost model](docs/cost-model.md) | What the sandbox costs a month, measured from the bill, and the levers |
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
- Bootstrap (`terraform init/plan`, service targets, destroy) lands in G1 under `infra/` — Platform DRI owns destroy/rebuild evidence; cost in [`docs/cost-model.md`](docs/cost-model.md)
