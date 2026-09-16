# Ownership matrix — devops-g9 TillFlow

**Gate:** G0 Decide · **Group:** 9 · **Prefix:** `devops-g9` · **Region:** `eu-north-1`  
**Rule:** Exactly one DRI per primary area. Every member owns ≥1 area and cross-reviews another. Ownership is shown via `owner` tags + `CODEOWNERS`, not resource names.

## Primary DRIs

| Primary area | DRI (name) | GitHub | Owns / decides | Minimum personal proof |
|---|---|---|---|---|
| **Product + POS** | Alice Moraa | [`@Moraaalice`](https://github.com/Moraaalice) | Tenant model, web flow, POS API, sale state, contracts | ADR + end-to-end sale demo |
| **Payments + integrity** | Mitingi Joy Chesang | [`@chesangJ`](https://github.com/chesangJ) | Daraja STK/B2C, callbacks, payment/payout state, idempotency, reconciliation, replay | Invariant tests + trace |
| **Platform + delivery** | Emebet Girmay | [`@emebetgirmay`](https://github.com/emebetgirmay) | Terraform, IAM, ECS, data services, cache, GHA, CodePipeline, scans | Plan + pipeline release |
| **Reliability + operations** | Emebet Girmay *(same as Platform — 3-person constraint)* | [`@emebetgirmay`](https://github.com/emebetgirmay) | SLIs/SLOs, budgets, ADOT/Grafana, k6, alerts, recovery, runbook | Dashboard + game day |

### Why Platform + Reliability share one DRI

The brief requires **four** named areas and **exactly one DRI each**, while our group has **three** members. Combining Platform + Reliability keeps a single accountable owner for infra, telemetry, pipelines, and recovery — the natural overlap. Product and Payments stay independent so money/state correctness has a dedicated defender.

Assignment above is **final** for the capstone unless changed in an explicit ownership PR.

## Cross-review map

| Author area | Mandatory reviewer (default) |
|---|---|
| Product + POS (`@Moraaalice`) | Payments + integrity (`@chesangJ`) |
| Payments + integrity (`@chesangJ`) | Product + POS (`@Moraaalice`) |
| Platform + delivery (`@emebetgirmay`) | Payments (`@chesangJ`) — default; Product (`@Moraaalice`) as backup |
| Reliability + operations (`@emebetgirmay`) | Payments (`@chesangJ`) on SLO/alert/runbook PRs |

## Path → DRI (must match `CODEOWNERS`)

| Path | DRI area |
|---|---|
| `services/web/`, `services/pos/` | Product + POS |
| `services/payments/`, `services/_shared/` (M-Pesa adapter) | Payments + integrity |
| `services/commission/` | Payments + integrity *(payout ledger + B2C via Payments API; Product reviews sale eligibility)* |
| `infra/`, `.github/workflows/` | Platform + delivery |
| `docs/slo-error-budgets.md`, `docs/runbook.md`, `evidence/reliability-ops/` | Reliability + operations |
| `docs/architecture.md`, `docs/adrs/`, `docs/threat-model.md` | Area DRI named in each ADR |

## Tag contract (every AWS resource)

```
group       = devops-g9
owner       = emebetgirmay | Moraaalice | chesangJ
environment = sandbox | staging | prod
service     = web | pos | payments | commission | shared | platform
managed-by  = terraform
capstone    = tillflow
```

## Sign-off (G0)

| Member | Primary area(s) | Cross-reviews | Signed (date) |
|---|---|---|---|
| Alice Moraa (`@Moraaalice`) | Product + POS | Payments | *(confirm in PR comment — pending)* |
| Mitingi Joy Chesang (`@chesangJ`) | Payments + integrity | Product | *(confirm in PR comment — pending)* |
| Emebet Girmay (`@emebetgirmay`) | Platform + delivery; Reliability + ops | Payments (default) on infra/SLO PRs | 2026-09-14 |
