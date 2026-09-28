# Evidence: Commission close to one B2C payout

DRI: Mitingi Joy Chesang (`@chesangJ`)

## G2: close.py then disburse.py against Payments `POST /payouts`, run twice

`collect.sh` seeds POS over its own API (one attendant at 5%, three paid sales of 2 x KSh 80),
runs Commission's real `close.py` and `disburse.py` against a running Payments service, then runs
both again for the same tenant and day. Fake adapter only: it never reaches Safaricom and needs no
credentials. The Daraja sandbox adapter and the daily schedule are not part of this gate.

```bash
(cd services/payments && DATABASE_URL=sqlite:////tmp/cp-payments.db python3 app.py) &
(cd services/pos && pip install -r requirements.txt && DATABASE_URL=sqlite:////tmp/cp-pos.db \
  PAYMENTS_BASE_URL=http://127.0.0.1:8080 uvicorn app.main:app --port 8081) &
PAYMENTS_DB=/tmp/cp-payments.db ./evidence/commission-payout/collect.sh
```

The script waits 61 s between the send pass and the reconcile pass: `disburse.py` reconciles right
after sending, sees `PENDING`, and backs off on the wall clock (`ledger/disburse.py`,
`BACKOFF_SECONDS`). In production the next scheduled pass settles it.

| File | Shows |
|---|---|
| `run1-close.json` | 3 paid sales seen, 1 attendant closed |
| `run1-disburse-send.json` | One `PLANNED` row sent: `POST /payouts` answered `created` |
| `run1-b2c-result.json` | The fake B2C result delivered and `applied` |
| `run1-disburse-reconcile.json` | Reconcile moved the row to `succeeded` |
| `run2-close.json` | Repeat close: same 3 sales seen, 0 attendants closed |
| `run2-disburse.json` | Repeat disburse: nothing to send, nothing to reconcile |
| `commission-ledger.json` | One `payout_ledger` row, 2400 minor, `SUCCEEDED` |
| `payout-final.json` | Payments `GET /payouts/{id}`: `SUCCEEDED`, `ledger_entries: 1` |
| `payments-db-counts.json` | Payments DB: 1 disbursement, 1 `DISBURSEMENT_DEBIT`, 2400 minor debited |
| `checks.json` | Pass or fail for each of the above; `collect.sh` exits non-zero if any fails |

The same pipeline is covered in-process by `services/commission/tests/test_end_to_end.py`.
