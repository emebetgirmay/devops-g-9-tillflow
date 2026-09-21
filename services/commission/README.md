# Commission worker (G2)

Group: devops-g9 · TillFlow · DRI: Mitingi Joy Chesang (`@chesangJ`)

Asks the Payments API to pay each payout that is due. **Commission never calls Daraja; it calls
the Payments service's `POST /payouts` only** ([ADR 0004](../../docs/adrs/0004-mpesa-adapter.md),
[ADR 0008](../../docs/adrs/0008-b2c-payouts.md)). It imports no M-Pesa code and holds no
credentials; `tests/test_no_daraja_guard.py` fails if that changes.

## Run

```bash
COMMISSION_INPUT=due.csv PAYMENTS_URL=http://127.0.0.1:8080 python3 worker.py
```

Input is a CSV with the columns `tenant_id,attendant_id,payout_period,msisdn,amount`, where
`amount` is an integer in minor units. Summing confirmed paid sales into that file is Product's
work and is not built here; the CSV is a stub for it.

## Behaviour

- The `Idempotency-Key` is derived from `(tenant, attendant, period)`, so running the worker again,
  or several workers at once, requests the same payout and gets the original answer. Payments
  derives its own `payout_key` and allows one live disbursement per payout.
- It never resubmits on its own. An unclear answer is left for the next run, which is safe because
  the key is the same.
- A paused payouts switch (503) or an unreachable Payments service stops the run.
- Amounts below KSh 10, above the ceiling, or not whole shillings come back 422 and are reported
  as held; carry-forward and holds are decided by Product.
- Exit codes: `0` fine, `1` a conflict or rejection needs a human, `2` bad input, `3` run aborted.

## Test

```bash
python3 -m unittest discover -s tests      # from this folder
```

The tests run the real Payments service in-process with the FakeAdapter.
