# Draft SLOs and error budgets — TillFlow

**Gate:** G0 draft; SLIs as implemented and capacity added at G3 · **Owner (Reliability DRI):** Emebet Girmay (`@emebetgirmay`) · **Window:** 28 days rolling  
**Rule:** Targets may change only before final benchmarking, with written rationale.

Budget = eligible events × (1 − target). Invalid requests and genuine business declines may be excluded; dependency outages still count when the user journey fails.

## Service budgets

| Service | Primary SLI | Starter target | 28-day error budget |
|---|---|---|---|
| **Web** | Eligible page/API-shell loads succeed | ≥ 99.9%; p95 < 500 ms | 0.1% ≈ 40m 19s |
| **POS API** | Valid sale writes accepted **exactly once** | ≥ 99.9%; p95 < 400 ms | 0.1% ≈ 40m 19s |
| **Payments API** | Valid STK/B2C accepted + callbacks processed within 60s | ≥ 99.5% | 0.5% ≈ 3h 21m 36s |
| **Commission** | Eligible payouts reach terminal state by **06:30 EAT** | ≥ 99.0%; duplicate disbursement = **0** | 1% events / 0.28 late runs |

## Definitions (draft — finalize by G3)

### Web
- **Numerator:** successful eligible loads (2xx within SLO)
- **Denominator:** eligible navigations/API-shell requests (exclude bots, health probes if tagged)
- **User outcome:** attendant can open till UI and start a sale

### POS API
- **Numerator:** unique successful sale creates (idempotency key → one sale row)
- **Denominator:** valid authenticated create attempts
- **Exclusions:** 4xx validation
- **User outcome:** sale recorded once, ready for payment

### Payments API
- **Numerator:** commands reaching accepted + callback/reconcile terminal within 60s
- **Denominator:** valid STK/B2C commands
- **Exclusions:** genuine Daraja business declines (customer cancel / insufficient funds) when correctly classified
- **Still counts:** timeouts that never reconcile within budget window if journey fails
- **User outcome:** payment state is truthful; timeout ≠ decline

### Commission
- **Numerator:** eligible payouts in terminal success/failed-with-reason by 06:30 EAT
- **Hard invariant:** duplicate B2C for same ledger row = 0 (any breach is a P0)
- **User outcome:** attendant paid once for confirmed paid sales

## SLIs as implemented at G3

Targets are unchanged from the G0 draft. What each SLI is measured from today
([ADR 0010](adrs/0010-reliability-observability.md), [ADR 0009](adrs/0009-payments-observability.md)):

| Service | SLI in production (G3) | Source | Alarms |
|---|---|---|---|
| **Edge** | Share of one-minute probes of the public `/ready` that return ready (uptime) | `TillFlow/Probe` `ProbeSuccess` | `probe-down` |
| **POS API** | 1 − 5xx ÷ requests at the ALB target group. The ALB's own health checks are not counted as requests; the edge probe's one `/ready` a minute is (a small, always-successful share) | ALB `HTTPCode_Target_5XX_Count`, `RequestCount` | `pos-fast-burn`, `pos-slow-burn`, `pos-down` |
| **Payments API** | Same, Payments target group; plus the money-path signals below | ALB, and Payments' `GET /metrics` | `payments-fast-burn`, `payments-slow-burn`, `payments-down`, ADR 0009 section 4 alarms |
| **Both** | 5xx the ALB generates itself (a target dropped the connection), which the target counts cannot see | ALB `HTTPCode_ELB_5XX_Count` | `alb-5xx-fast-burn`, `alb-5xx-slow-burn` |
| **Commission** | Payouts still unresolved: `payments_records` / `payments_oldest_age_seconds` for disbursements; duplicate payout = critical anomaly | Payments' metrics | `payments-payout-unknown-too-long`, `payments-critical-anomaly`, `payments-payouts-paused` |

**Interim, stated plainly:** the POS and Payments availability SLIs are measured at the ALB, not yet
from the services' own "valid write accepted exactly once" counters. Those counters now exist
(`pos_sale_creates_total`, `payments_create_results_total`) and are on the dashboards; moving the
burn alarms onto them is ADR 0010 work item R-6. The Payments 60-second window (callback or
reconcile to terminal) is observed through `payments_oldest_age_seconds`; its exact start point for
STK is still ADR 0009 open question 2.

**Burn alarms and the budget policy below:** fast burn is 14.4x the budget rate over 5 minutes,
which spends about 2% of the 28-day budget in an hour (the policy's fast-burn example); slow burn
is 6x over 30 minutes.

## Budget policy (draft)

| Signal | Action |
|---|---|
| Fast burn (e.g. 2% budget in 1h) | Page Reliability DRI; freeze risky deploys; investigate |
| Slow burn (e.g. budget on track to exhaust in <3 days) | Alert; prefer roll-forward fixes; no feature work that risks SLO |
| Budget exhausted | Feature freeze until burn rate cools and postmortem note in `scar-log.md` |
| Resume | Reliability + area DRI agree; link Grafana burn panel |

## Capacity (G3, k6 against the fake adapter)

Measured by [`evidence/reliability-ops/k6-analysis.md`](../evidence/reliability-ops/k6-analysis.md).
Thresholds: failed < 1%, p95 < 500 ms, checks > 99%, CPU < 70%, memory < 75%.

| | Value |
|---|---|
| Load model | Smoke, stepped 5 → 10 → 20 VUs, spike 50 VUs, 15-minute soak at 10 VUs; 80% payments / 20% payouts; a driver delivering callbacks and reconciling |
| Task size and count | Payments: **1 task, 0.5 vCPU, 1 GB** (raised from 0.25 vCPU / 512 MB at G3) |
| Highest sustained rate | **36.7 requests/s** over 17 minutes, all thresholds met: failed 0.00%, p95 154 ms, checks 100% |
| Headroom | CPU average 26%, peak 55% (spike) against the 70% line; memory 22% |
| Scaling metric | CPU (`payments-cpu-high` at 70%); vertical only on SQLite, see below |
| Bottleneck | Payments CPU and SQLite's single writer; the spike's long tail (max 7.6 s) is writers queueing |

Not production sizing: SQLite serialises writers and each task has its own database, so horizontal
scaling waits for Postgres/RDS (ADR 0002). At 0.25 vCPU the same load saturated CPU (99.75%) and
p95 missed the target in one of two runs; that is why Payments was resized.

## Next gate

G3 (done, see [`g3-evidence.md`](../evidence/reliability-ops/g3-evidence.md)) required Grafana 5m/1h/28d uptime, SLO target, budget remaining, burn rate, RED, saturation, business signals + Slack firing/recovery.
