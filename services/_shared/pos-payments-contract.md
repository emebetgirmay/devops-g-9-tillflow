# POS ↔ Payments contract

**Status:** agreed by Product+POS, drafted here for Payments to confirm before implementing.
**Owners:** Alice Moraa (`@Moraaalice`, POS side) · Mitingi Joy Chesang (`@chesangJ`, Payments side)

This is the interface between `services/pos` and `services/payments` referenced in
`docs/architecture.md`. POS is always the caller of the outbound request; Payments is always
the caller of the callback. Both are internal service-to-service calls (not exposed through
API Gateway — see `docs/threat-model.md`).

## 1. POS → Payments: request a payment

POS calls this once a sale reaches `READY_FOR_PAYMENT` and the attendant/customer is ready to
pay. Implemented on the POS side at `services/pos/app/payments_client.py` /
`POST /tenants/{tenant_id}/sales/{sale_id}/payment-request` (POS's own endpoint, which then
calls Payments internally).

```
POST {PAYMENTS_BASE_URL}/payments
```

Request body:

```json
{
  "sale_id": "b6c1...-uuid",
  "tenant_id": "9fe6...-uuid",
  "amount_minor": 24000,
  "currency": "KES",
  "phone": "254712345678",
  "idempotency_key": "b6c1...-uuid"
}
```

Notes:
- `idempotency_key` is always the `sale_id` — one sale can only ever have one in-flight
  payment. Payments must treat repeat calls with the same `idempotency_key` as returning the
  same (or a fresh, if the previous one failed) payment rather than creating a duplicate STK
  push, mirroring how POS treats sale creation.
- `amount_minor` is server-computed by POS from the tenant's product catalog — never trust a
  client-supplied price. Payments should treat this value as authoritative for the STK amount.

Expected response (`2xx`):

```json
{
  "payment_id": "pay-uuid",
  "status": "PENDING"
}
```

POS stores `payment_id` on the sale and moves the sale to `PAYMENT_REQUESTED`. If this call
fails or times out, POS's own state stays `READY_FOR_PAYMENT` (no transition applied — see
`services/pos/app/routers/sales.py::request_payment`), so retrying is always safe: **a timeout
here must not be recorded as a payment**, matching "a timeout is not a decline" from the brief.

## 2. Payments → POS: report a payment outcome

Payments calls this once STK completes, is confirmed via query/reconcile, or B2C-adjacent flows
resolve — whenever a sale's payment reaches a terminal outcome. Implemented on the POS side at

```
POST {POS_BASE_URL}/internal/sales/{sale_id}/payment-events
```

Request body:

```json
{
  "event_id": "evt-uuid",
  "payment_id": "pay-uuid",
  "status": "PAID",
  "amount_minor": 24000,
  "occurred_at": "2026-09-21T10:00:00Z"
}
```

- `status` is one of `PAID` | `PAYMENT_FAILED` — POS has no `PENDING`-facing state for this
  endpoint; a pending/uncertain payment is simply not reported yet.
- `event_id` is **Payments' own idempotency key for this specific event**, distinct from
  `payment_id` — one payment can produce more than one event over its lifecycle (e.g. an
  STK timeout followed by a query/reconcile correction). POS deduplicates on `event_id`: the
  same `event_id` delivered twice is a no-op (`applied: false`, sale status unchanged).
- `amount_minor` must match the sale's total exactly, or POS rejects with `409` — this is a
  cross-check against tampering or a Payments-side bug, not just a formality.

Response:

```json
{
  "sale_id": "b6c1...-uuid",
  "status": "PAID",
  "applied": true
}
```

`applied: false` means this call caused no new effect — either the `event_id` was already
seen, or the transition was illegal for the sale's current status (e.g. a reordered
`PAYMENT_FAILED` arriving after the sale already reached `PAID`). Either way POS still records
the event for the audit trail/trace; it just never overwrites a settled sale. **Reordering or
replaying callbacks must always produce exactly one legal transition and one ledger effect** —
this endpoint is where POS enforces that on its side of the boundary.

Sale state machine on the POS side (for reference — POS owns this, Payments doesn't need to
replicate it, just knows PAID/PAYMENT_FAILED are the two terminal-ish outcomes it can report):

```
READY_FOR_PAYMENT --(payment-request)--> PAYMENT_REQUESTED --(PAID event)--> PAID  [terminal]
                                                 |
                                                 +--(PAYMENT_FAILED event)--> PAYMENT_FAILED
                                                        |
                                                        +--(payment-request retry)--> PAYMENT_REQUESTED
```

`VOID` exists on the POS side for pre-payment cancellation and is not reachable from
Payments-originated events.

## Open items for Payments to confirm

- Exact base path/port Payments will listen on internally (`PAYMENTS_BASE_URL` POS reads from
  env — Platform to wire the actual service-discovery address in Terraform).
- Whether Payments needs anything else from POS in the request body (e.g. till_id) — everything
  Payments should need to drive Daraja is in the body above; add fields here rather than
  inventing them ad hoc once implementation starts.
