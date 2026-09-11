# ADR 0004 — M-Pesa adapter and sandbox boundary

- **Status:** Proposed (G0)
- **Date:** 2026-09-09
- **DRI:** Payments + integrity — Mitingi Joy Chesang (`@chesangJ`)

## Context

Payments owns Daraja. CI and k6 must never spend real money or touch customer data. Commission must not call Daraja directly.

## Decision

- Define `MpesaPort` (auth, STK push, query, B2C, callback parse) in `services/_shared/`
- **FakeAdapter**: deterministic, clock-injectable, supports timeout / duplicate callback / decline scenarios for tests and k6
- **DarajaSandboxAdapter**: used only in deployed sandbox env with Secrets Manager creds (`devops-g9/daraja`)
- Commission requests B2C **only** through Payments API
- Timeout → pending + query/reconcile; never map timeout to decline

## Consequences

- Contract/replay tests live under Payments (+ shared)
- Small live sandbox contract test is separate from load tests
- No Safaricom secrets in git

## Alternatives

- Calling Daraja from Commission — rejected (brief forbids)
- Real Daraja in k6 — rejected
