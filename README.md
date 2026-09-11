# devops-g9 — TillFlow

Multi-tenant POS + M-Pesa (Daraja **sandbox**) on AWS ECS. Private group mono-repo · Terraform · GitHub Actions + CodePipeline.

**Due:** Mon 21 Sep 2026, 23:59 EAT · **Prefix:** `devops-g9` · **Region:** `eu-north-1` (see `docs/adrs/0001-aws-region.md`)

## Today's gate: G0 — Decide (D2 · 9 Sep 2026)

Pass evidence:

- [x] Private mono-repo scaffold (this repo)
- [x] Teammates invited (Write access)
- [ ] Mentor access invited
- [x] Ownership matrix filled with real names (`docs/ownership.md`)
- [x] Architecture (`docs/architecture.md`)
- [x] ADRs incl. region (`docs/adrs/` — region = `eu-north-1`)
- [x] Threat model (`docs/threat-model.md`)
- [x] Draft SLOs (`docs/slo-error-budgets.md`)
- [x] `CODEOWNERS` handles updated

Blocked if any member has no primary area, or a critical decision has no DRI.

## Ownership (3 members → 4 areas)

| Area | DRI |
|---|---|
| Product + POS | Alice Moraa (`@Moraaalice`) |
| Payments + integrity | Mitingi Joy Chesang (`@chesangJ`) |
| Platform + delivery | Emebet Girmay (`@emebetgirmay`) |
| Reliability + operations | Emebet Girmay (combined — see ownership.md) |

## Layout

```
/
├─ services/web|pos|payments|commission|_shared
├─ infra/                 # Terraform
├─ .github/workflows/     # PR checks + gated apply
├─ docs/                  # ownership, architecture, ADRs, SLOs, threat model, …
├─ evidence/<area>/       # runtime proof per DRI
└─ CODEOWNERS
```

## Rules

- Deploy **only** in the assigned region; name `devops-g9-…`; tag `group`, `owner`, `environment`, `service`, `managed-by=terraform`, `capstone=tillflow`
- Daraja sandbox only — never commit real money flows’ credentials or customer data
- CI/k6 use deterministic fake M-Pesa adapter

## Gates

| Gate | Due | Focus |
|---|---|---|
| G0 Decide | D2 (9 Sep) | Ownership, architecture, ADRs, threat model, draft SLOs |
| G1 Platform | D5 (12 Sep) | Terraform apply, ECS golden path, first pipeline |
| G2 Product | D8 (15 Sep) | Sale → STK → paid; commission → B2C |
| G3 Operate | D11 (18 Sep) | Grafana, k6, Slack alerts |
| G4 Recover | D13 (20 Sep) | Failure drills + restore |
| G5 Release | D14 (21 Sep) | Fresh release + defences |

## Bootstrap (later gates)

```bash
# TBD G1: terraform init/plan, service make targets, destroy
```

## Cost / cleanup

Tracked at G5; Platform DRI owns destroy/rebuild evidence.
