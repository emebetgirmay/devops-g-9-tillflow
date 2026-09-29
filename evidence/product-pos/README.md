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
- [x] End-to-end sale demo against deployed sandbox (Sale -> STK callback -> paid). Run for real
  against the live API Gateway URL on 2026-09-29: `sale-demo-create.json` (sale created,
  `READY_FOR_PAYMENT`) -> `sale-demo-payment-request.json` (STK requested) ->
  `sale-demo-fake-deliver.json` (Payments' deterministic fake STK callback, standing in for
  Daraja) -> `sale-demo-reconcile-1.json` (`PAID` on the first poll) -> `sale-demo-final.json`.
  `sale-demo-reconcile-replay.json` proves a repeat reconcile is a no-op (`applied: false`).

### Reproduction

```bash
cd services/pos
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest --cov=app --cov-report=term-missing
```

**End-to-end sale demo against the deployed sandbox** —
`evidence/product-pos/demo-sale-to-paid.sh`. Creates a tenant/sale, requests payment, drives
Payments' fake STK callback, and loops `payment-reconcile` until `PAID` (or fails loudly after 5
tries) — saving every response as `sale-demo-*.json` in this directory:

```bash
aws sso login --profile g9   # or: aws login --profile g9
AWS_PROFILE=g9 ./evidence/product-pos/demo-sale-to-paid.sh
```

Manual smoke, local only (see `services/pos/README.md` for the full endpoint list):

```bash
.venv/bin/uvicorn app.main:app --port 8080 &
curl -s localhost:8080/health
TID=$(curl -s -X POST localhost:8080/tenants -d '{"name":"Demo Duka"}' -H 'content-type: application/json' | python3 -c "import sys,json;print(json.load(sys.stdin)['id'])")
# ...create till/attendant/product, then POST a sale with Idempotency-Key — see README.
```

## Later
- Live trace (sale -> payment-request -> payment-event) once ADOT/Payments are wired end to end.
- Cross-review sign-off from Payments DRI on `services/_shared/pos-payments-contract.md`.
