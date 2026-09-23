# Commission worker (G2)

Group: devops-g9 · TillFlow · DRI: Mitingi Joy Chesang (`@chesangJ`)

Turns POS's confirmed-paid sales into B2C payouts. **Commission never calls Daraja; it calls the
Payments service's `POST /payouts` only**
([ADR 0004](../../docs/adrs/0004-mpesa-adapter.md), [ADR 0008](../../docs/adrs/0008-b2c-payouts.md)).
It imports no M-Pesa code and holds no credentials; `tests/test_no_daraja_guard.py` fails the
build if that changes. It also never touches POS's database directly — only POS's own read-only
`GET /tenants/{id}/commission/paid-sales` endpoint
(`services/_shared/pos-commission-contract.md`).

## The pipeline

```
POS confirmed-paid sales  --close.py-->  payout_ledger, payout_items  --disburse.py-->  Payments
     (per-tenant, read-only HTTP)         (this service's own sqlite)      (POST /payouts,
                                                                             POST .../reconcile)
```

1. **`close.py`** (daily close, ADR 0008 sections 1-2): for one tenant and one business day, pulls
   every `PAID` sale POS reports, records one `payout_items` row per sale with its computed
   commission, and folds each attendant's total into a `payout_ledger` row. Safe to run more than
   once for the same day.
2. **`disburse.py`** (Commission -> B2C): sends every `PLANNED` ledger row to Payments, then
   reconciles the ones already `REQUESTED` to a terminal state. Safe to run repeatedly or
   concurrently.

Both are meant to run on a schedule (cron or EventBridge — wiring is Platform's,
[ADR 0006](../../docs/adrs/0006-idempotency-replay.md) open question 9); each pass is idempotent,
so an overlapping or repeated run changes nothing extra.

```bash
COMMISSION_TENANT_IDS=t1,t2 python3 close.py     # closes yesterday (EAT) for every tenant
python3 close.py --business-date 2026-09-20 --tenant t1
python3 disburse.py
```

## The ledger (`ledger/`, sqlite by default)

| Table | Holds |
|---|---|
| `payout_items` | One row per sale, `UNIQUE(sale_id)` — a sale is counted at most once no matter how often close runs |
| `payout_ledger` | One row per `(tenant, attendant, business_date)` that actually got paid; `UNIQUE` and non-`FAILED` per that triple |
| `carry_forward` | The exact remainder (sub-shilling and sub-minimum) not yet in any ledger row — never dropped, always folded into the next close that pays that attendant |
| `close_runs`, `anomalies` | Audit trail |

Money-safety rules `ledger/calc.py` and `ledger/close.py` implement (judgment calls where ADR 0008
left the business rule open — see the ADR's open questions):

- **Business day**: a plain EAT (UTC+3) calendar day, no special cutoff hour. A sale is attributed
  to the day its `PAID` transition happened on (`paid_at` from POS, which is `sale.updated_at`).
- **Rounding**: floors only, at the per-sale step and the whole-shilling step, so a rounding error
  can only ever underpay relative to the exact bps share, never overpay.
- **Below the KSh 10 minimum**, or a whole-shilling floor that leaves nothing at or above it,
  accrues in `carry_forward` rather than attempting (and failing) a disbursement. It clears the
  next time that attendant's commission, added to what carried forward, crosses the minimum —
  not purely by calendar day, so an attendant with zero sales on a given day does not force a
  payout of just their old carried amount that day.

`ledger/disburse.py` reads Payments' response, not just its HTTP status: a `201` can already carry
a terminal verdict (a synchronous rejection comes back `FAILED` in the same response; a
disburse-time timeout or duplicate-id answer comes back `UNKNOWN`, landing the ledger row in
`REQUESTED` so `reconcile` can poll it — never resubmitted). A documented `INSUFFICIENT_FUNDS` or
configuration result fails that one row and relies on Payments' own kill switch to stop the rest
of the run; nothing here retries a failed row automatically — a fresh `PLANNED` row (a new
business day, or an operator's explicit action) gets its own key and its own attempt.

## Legacy CSV path (`worker.py`)

The original stub, kept for a manual or one-off correction: a CSV of
`tenant_id,attendant_id,payout_period,msisdn,amount` sent straight to `POST /payouts`, bypassing
the ledger entirely. `close.py`/`disburse.py` are the real path now; `worker.py`'s idempotency-key
derivation and Payments-response classification live in `ledger/common.py` so both paths agree.

```bash
COMMISSION_INPUT=due.csv PAYMENTS_URL=http://127.0.0.1:8080 python3 worker.py
```

## Test

```bash
python3 -m unittest discover -s tests                       # 53 tests, stdlib only
pip install -r ../pos/requirements.txt                       # optional, for the two below
python3 -m unittest discover -s tests -p test_end_to_end.py  # +2 tests: real POS + Payments + Commission
```

`tests/test_calc.py` is pure unit tests. `tests/test_close.py` runs the real close logic against a
deterministic stand-in HTTP server for POS's contract (`tests/fake_pos.py`). `tests/test_disburse.py`
and `tests/test_worker.py` run against the real Payments service in-process, `FakeAdapter` only.
`tests/test_end_to_end.py` runs all three real service codebases together over real HTTP — skipped
automatically unless POS's dependencies are installed.
