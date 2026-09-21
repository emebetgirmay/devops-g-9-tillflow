# ADR 0004 — M-Pesa adapter and sandbox boundary

- **Status:** Proposed (G0)
- **Date:** 2026-09-09
- **DRI:** Payments + integrity — Mitingi Joy Chesang (`@chesangJ`)
- **Related:** [ADR 0006](0006-idempotency-replay.md) (payment states, idempotency, timeout and callback-replay rules that run behind this port)

## Context

Payments owns Daraja. CI and k6 must never spend real money or touch customer data. Commission must not call Daraja directly.

## Decision

- Define `MpesaPort` (auth, STK push, query, B2C, callback parse) in `services/_shared/`
- **FakeAdapter**: deterministic, clock-injectable, supports timeout / duplicate callback / decline scenarios for tests and k6
- **DarajaSandboxAdapter**: used only in deployed sandbox env with Secrets Manager creds (`devops-g9/daraja`)
- Commission requests B2C **only** through Payments API
- Timeout → pending + query/reconcile; never map timeout to decline (states, backoff and replay rules: [ADR 0006](0006-idempotency-replay.md))

### Boundary rules (clarification of the decisions above)

- **Commission never calls Daraja.** It has no Daraja client, URL or credential. Its only path to money movement is the Payments API.
- **Only the adapter implementation talks to Daraja.** All other Payments code calls `MpesaPort`. In deployed sandbox that implementation is `DarajaSandboxAdapter`.
- **CI and k6 use the FakeAdapter only.** No workflow, test or load script may use the sandbox adapter or reach Safaricom.
- **Real credentials never live in Commission or in CI.** `devops-g9/daraja` is used only by the deployed Payments sandbox adapter (least-privilege task roles, see the [threat model](../threat-model.md)). Nothing goes in git, fixtures, tests or docs.
- **Callbacks and reconciliation go through the port too.** Callback parsing and status queries use the same port, and their idempotency and replay behaviour is specified in [ADR 0006](0006-idempotency-replay.md).

## Consequences

- Contract/replay tests live under Payments (+ shared)
- Small live sandbox contract test is separate from load tests
- No Safaricom secrets in git
- Correctness of duplicate, late and out-of-order callbacks is decided in [ADR 0006](0006-idempotency-replay.md) and proven with the FakeAdapter

## Alternatives

- Calling Daraja from Commission — rejected (brief forbids)
- Real Daraja in k6 — rejected
