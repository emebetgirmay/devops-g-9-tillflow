# POS ↔ Payments contract

**Status:** implemented and integration-tested (both sides, live, deterministic — see below).
**Owners:** Alice Moraa (`@Moraaalice`, POS side) · Mitingi Joy Chesang (`@chesangJ`, Payments side)

This is the interface between `services/pos` and `services/payments`. POS is always the caller —
Payments has **no outbound callback to POS**; POS polls for the outcome. That matches the brief's
"a timeout is not a decline — keep it pending, query/reconcile" directly.

The wire format below is Joy's actual, shipped `services/payments` API (`core/payments.py`,
`core/config.py`), verified by running both services together — this doc originally guessed at a
push-callback design before that branch was pulled; this version replaces it.

## 1. POS → Payments: request a payment

`services/pos/app/payments_client.py::request_payment`, called from
`POST /tenants/{tenant_id}/sales/{sale_id}/payment-request` (POS's own endpoint).

```
POST {PAYMENTS_BASE_URL}/payments
Idempotency-Key: <sale_id>          (header, not a body field — 16-64 chars [A-Za-z0-9_-])
```

```json
{
  "tenant_id": "9fe6...-uuid",
  "sale_id": "b6c1...-uuid",
  "msisdn": "254712345678",
  "amount": 24000,
  "currency": "KES",
  "account_reference": "b6c1...uu"
}
```

- `Idempotency-Key` is always the `sale_id` — one sale, one in-flight payment. Payments treats a
  repeat with the same key + same payload as a replay (returns the original response); a
  different payload under the same key is a `409`.
- `amount` is minor units, server-computed by POS from the tenant's catalog — never trust a
  client-supplied price.
- `account_reference` ≤ 12 chars (POS sends `sale_id[:12]`).

Response (`201`, or `200` on idempotent replay):

```json
{"payment_id": "pay_...", "checkout_request_id": "...", "state": "PENDING", "decline_reason": null}
```

POS stores `payment_id` on the sale and moves it to `PAYMENT_REQUESTED`. A failed/timed-out call
here leaves the sale at `READY_FOR_PAYMENT` untouched — retrying is always safe.

## 2. POS → Payments: find out how it settled (the active integration path)

`services/pos/app/payments_client.py::get_payment`, called from
`POST /tenants/{tenant_id}/sales/{sale_id}/payment-reconcile` (POS's own endpoint — POS-initiated,
safe to call repeatedly, including while still pending).

```
GET {PAYMENTS_BASE_URL}/payments/{payment_id}
```

```json
{
  "payment_id": "pay_...", "checkout_request_id": "...", "state": "SUCCEEDED",
  "decline_reason": null, "tenant_id": "...", "sale_id": "...",
  "amount_minor": 24000, "currency": "KES", "msisdn": "2547****678",
  "receipt": "...", "ledger_entries": 1
}
```

`state` is one of `CREATED | PENDING | UNKNOWN | NEEDS_REVIEW | SUCCEEDED | DECLINED | EXPIRED`
(`services/payments/core/states.py::PaymentState`). POS maps only the terminal ones:

| Payments `state` | POS sale status |
|---|---|
| `SUCCEEDED` | `PAID` |
| `DECLINED`, `EXPIRED` | `PAYMENT_FAILED` |
| `CREATED`, `PENDING`, `UNKNOWN`, `NEEDS_REVIEW` | *(no change — not a verdict yet)* |

POS also checks `amount_minor` against the sale's own total before applying — a mismatch is a
`409`, not silently trusted. Applying is idempotent per `(payment_id, state)`: polling again once
settled is a no-op (`applied: false`), and a stale/reordered non-matching state polled after the
sale already reached a terminal status is rejected, never overwrites it.

**There is no push callback in this build.** `POST /internal/sales/{sale_id}/payment-events` still
exists on the POS side (`app/routers/internal.py`) sharing the same idempotent apply logic, in
case a push model (e.g. Payments → SQS/EventBridge → POS) gets wired later by Platform — but
nothing calls it today. Whoever polls (a POS-side scheduler, or the web frontend after STK push)
is an open wiring question — see below.

## Verified live (deterministic, no mocking)

Ran both real services together (`services/pos` + `services/payments`, Payments'
`FAKE_CLOCK=manual`) through: create sale → `payment-request` → `/_fake/advance` +
`/_fake/deliver-callbacks` (Payments' own deterministic driver, not Daraja) → `payment-reconcile`.
Result: sale reached `PAID`, a repeat `payment-reconcile` came back `applied: false` — this is
G2's literal pass criterion ("Sale → STK callback → paid"), done for real.

## Open items

- **Who calls `payment-reconcile` and when?** Nobody schedules it yet — needs either a POS-side
  poller/cron, or the web frontend calling it after STK push, or Platform wiring an
  EventBridge-triggered sweep. Not decided.
- **Commission input gap**: `services/commission/worker.py` takes a pre-aggregated CSV
  (`tenant_id, attendant_id, payout_period, msisdn, amount`) — it does **not** read POS's sales at
  all. Its own docstring says computing commission from confirmed-paid sales is "Product's work
  and is not built here." Nothing currently builds that CSV from POS data. POS doesn't yet expose
  an endpoint to list/aggregate paid sales per attendant either — needed either way.
