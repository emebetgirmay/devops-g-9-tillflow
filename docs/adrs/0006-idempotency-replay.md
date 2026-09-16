# ADR 0006 — Idempotency and callback replay safety

- **Status:** Proposed (G0 follow-up — fill before G2)
- **Date:** TBD
- **DRI:** Payments + integrity — Mitingi Joy Chesang (`@chesangJ`)

## Context

Timeouts are not declines. Callbacks may duplicate or reorder. Commission must never double-pay.

## Decision

*(Joy: document the payment/payout state machine, idempotency keys, legal transitions, and how FakeAdapter proves replay.)*

## Consequences

*(Joy)*

## Alternatives considered

*(Joy)*
