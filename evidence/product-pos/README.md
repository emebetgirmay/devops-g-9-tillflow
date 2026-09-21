# Evidence — Product + POS

DRI: Alice Moraa (`@Moraaalice`)

## G0
- [x] Linked ADR / architecture decisions you own — `docs/adrs/0007-multi-tenancy.md`
- [ ] PR link for G0 docs contribution

## G2 — Product

- [x] POS API implemented (`services/pos/app/`): tenant/till/attendant/product setup, idempotent
  sale creation with server-computed totals, sale state machine, payment-request +
  payment-event integration points.
- [x] POS ↔ Payments contract agreed and written down —
  `services/_shared/pos-payments-contract.md`.
- [x] Invariant tests (`services/pos/tests/`): tenant-scoped validation, idempotent create
  (incl. concurrent-retry race), replayed/reordered payment-callback safety
  (`test_payment_events.py` — one legal transition, one ledger effect), amount-mismatch
  rejection. `pytest --cov=app` at 93% line coverage (floor is 70%).
- [ ] End-to-end sale demo against deployed sandbox (Sale -> STK callback -> paid), once
  Payments lands and Platform wires `DATABASE_URL`/`PAYMENTS_BASE_URL`.

### Reproduction

```bash
cd services/pos
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest --cov=app --cov-report=term-missing
```

Manual smoke (see `services/pos/README.md` for the full endpoint list):

```bash
.venv/bin/uvicorn app.main:app --port 8080 &
curl -s localhost:8080/health
TID=$(curl -s -X POST localhost:8080/tenants -d '{"name":"Demo Duka"}' -H 'content-type: application/json' | python3 -c "import sys,json;print(json.load(sys.stdin)['id'])")
# ...create till/attendant/product, then POST a sale with Idempotency-Key — see README.
```

## Later
- Live trace (sale -> payment-request -> payment-event) once ADOT/Payments are wired end to end.
- Cross-review sign-off from Payments DRI on `services/_shared/pos-payments-contract.md`.
