# POS ↔ Commission contract

**Status:** implemented and tested (both sides — see below).
**Owners:** Alice Moraa (`@Moraaalice`, POS side) · Mitingi Joy Chesang (`@chesangJ`, Commission side)

The interface between `services/pos` and `services/commission`. POS is always the source; it
exposes one read-only endpoint and Commission is the only caller. This fills the "Commission input
gap" `pos-payments-contract.md` names: Commission calculates only from confirmed **paid** sales
([architecture.md](../../docs/architecture.md) invariant 4), and only POS knows which sales are
`PAID` and what each attendant's commission rate is.

## POS -> Commission: list paid sales

`services/pos/app/routers/commission.py`, called from `services/commission/ledger/pos_client.py`.

```
GET /tenants/{tenant_id}/commission/paid-sales?since=<ISO8601>&until=<ISO8601>&attendant_id=<opt>&limit=<opt>&cursor=<opt>
```

```json
{
  "sales": [
    {
      "sale_id": "b6c1...-uuid",
      "attendant_id": "7a9b...-uuid",
      "attendant_msisdn": "254712345678",
      "commission_rate_bps": 500,
      "total_minor": 16000,
      "currency": "KES",
      "paid_at": "2026-09-21T09:00:00+00:00"
    }
  ],
  "next_cursor": null
}
```

- Read-only: this endpoint cannot move a sale out of `PAID` or write anything.
- Only sales with `status == PAID` are returned. `paid_at` is `sale.updated_at` — reliable because
  `PAID` is terminal (`app/state.py` has no transitions out of it), so it never changes again.
- `commission_rate_bps` and `attendant_msisdn` are joined from `Attendant` so Commission never has
  to look POS's own attendant record up separately: POS owns that data, Commission owns the ledger.
- `since` is inclusive, `until` is exclusive. `limit` defaults to 500 (POS caps it at 2000).
- Pagination is a keyset cursor on `(updated_at, id)`, not an offset, so a page boundary landing on
  two sales with the same timestamp never skips or repeats one. Follow pages until `next_cursor`
  is `null`. A stray duplicate page is safe on Commission's side either way —
  `payout_items.sale_id` is `UNIQUE`.
- Errors: `404` for an unknown tenant, `400` for `until <= since` or a malformed cursor.

## Verified live (deterministic, no mocking)

Ran all three real services together (`services/pos` + `services/commission` +
`services/payments`, Payments' `FakeAdapter`) through: create tenant/till/attendant/product ->
three paid sales over real HTTP -> Commission's real daily close -> Commission's real disburse and
reconcile -> Payments' real ledger. Result: one `payout_ledger` row at the correct commission
amount, one Payments ledger entry, and running the whole pipeline a second time added nothing —
no second ledger row, no second disbursement call, no second debit
(`services/commission/tests/test_end_to_end.py`).

## Open items

- **Business day and carry-forward are Commission-side judgment calls**, not part of this
  contract: see `services/commission/README.md` and
  [ADR 0008](../../docs/adrs/0008-b2c-payouts.md) open questions 3 and 4 (owned by `@Moraaalice`).
- **Scheduling.** Nobody calls `close.py`/`disburse.py` on a timer yet — same open item
  `pos-payments-contract.md` already names for `payment-reconcile`
  ([ADR 0006](../../docs/adrs/0006-idempotency-replay.md) open question 9, `@emebetgirmay`).
- **No authentication** on this endpoint, matching every other POS route today.
