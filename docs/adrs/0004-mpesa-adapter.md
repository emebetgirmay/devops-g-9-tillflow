# ADR 0004 — M-Pesa adapter and sandbox boundary

- **Status:** Accepted (G2)
- **Date:** 2026-09-09
- **Accepted:** 2026-09-21 (G2; approved by `@Moraaalice` and `@emebetgirmay`)
- **DRI:** Payments + integrity — Mitingi Joy Chesang (`@chesangJ`)
- **Related:** [ADR 0006](0006-idempotency-replay.md) (payment states, idempotency, timeout and callback-replay rules that run behind this port), [ADR 0008](0008-b2c-payouts.md) (B2C payouts through this boundary)

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

### Implementation notes (G2)

The decision stands. What shipped, and where it differs from the wording above:

- **Two ports, not one.** `services/_shared/mpesa/` has `MpesaPort` (STK charge, status query, callback parse) and `DisbursementPort` (B2C disburse, status query, result parse; see [ADR 0008](0008-b2c-payouts.md)). Token authentication is an implementation detail of a real adapter, not a port method.
- **FakeAdapter** lives in `services/_shared/mpesa/`: deterministic, clock-injectable, with documented magic-number scenarios (see the folder README). A guard test fails on any HTTP client import, Safaricom URL or secret pattern in that folder.
- **DarajaSandboxAdapter is not built.** `services/payments` wires the FakeAdapter only, and any other `MPESA_ADAPTER` value makes the service refuse to start; it never falls back silently. The deployed sandbox adapter and its contract test remain to do, with Platform-managed secrets (`devops-g9/daraja`; [ADR 0006](0006-idempotency-replay.md) open question 12 proposes a separate API user and secret per Daraja role).
- **Commission** (`services/commission/worker.py`) calls the Payments `POST /payouts` endpoint only. A guard test fails if it imports the M-Pesa package, holds a Safaricom URL or carries a secret. Payments has its own guard against outbound HTTP clients.
- **Provider references.** STK uses the provider's `CheckoutRequestID`; B2C uses a caller-chosen `OriginatorConversationID` ([ADR 0008](0008-b2c-payouts.md)).
- **Result codes are per API.** `result_codes.py` (STK; only codes 0 and 1032 are verified) and `b2c_result_codes.py` (B2C) must never share a table.

## Consequences

- Contract/replay tests live under Payments (+ shared)
- Small live sandbox contract test is separate from load tests
- No Safaricom secrets in git
- Correctness of duplicate, late and out-of-order callbacks is decided in [ADR 0006](0006-idempotency-replay.md) and proven with the FakeAdapter

## Alternatives

- Calling Daraja from Commission — rejected (brief forbids)
- Real Daraja in k6 — rejected
