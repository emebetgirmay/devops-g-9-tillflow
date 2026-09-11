# Draft SLOs and error budgets — TillFlow

**Gate:** G0 draft · **Owner (Reliability DRI):** Emebet Girmay (`@emebetgirmay`) · **Window:** 28 days rolling  
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

## Budget policy (draft)

| Signal | Action |
|---|---|
| Fast burn (e.g. 2% budget in 1h) | Page Reliability DRI; freeze risky deploys; investigate |
| Slow burn (e.g. budget on track to exhaust in <3 days) | Alert; prefer roll-forward fixes; no feature work that risks SLO |
| Budget exhausted | Feature freeze until burn rate cools and postmortem note in `scar-log.md` |
| Resume | Reliability + area DRI agree; link Grafana burn panel |

## Capacity (placeholder until k6 @ G3)

- Load model, task size/count, scaling metric, headroom, highest sustainable RPS — fill after smoke / stepped / spike / ≥15m soak against **fake** M-Pesa adapter.
- Thresholds: failed < 1%, p95 < 500 ms, checks > 99%, CPU < 70%, memory < 75%, bounded queue age.

## Next gate

G3 requires Grafana 5m/1h/28d uptime, SLO target, budget remaining, burn rate, RED, saturation, business signals + Slack firing/recovery.
