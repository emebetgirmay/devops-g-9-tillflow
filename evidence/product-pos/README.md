# Evidence — Product + POS

DRI: Alice Moraa (`@Moraaalice`)

## G0
- [x] Linked ADR / architecture decisions you own — `docs/adrs/0007-multi-tenancy.md`
- [x] PR link for G0 docs contribution — [#14](https://github.com/emebetgirmay/devops-g-9-tillflow/pull/14)
  (ADR 0007 filled in and accepted, alongside the G2 sale API it documents)

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

## G3 — Operate

- [x] `GET /metrics` (ADR 0010, work item P-1) implemented in `services/pos/app/metrics.py`:
  five Prometheus metrics (`pos_http_requests_total`, `pos_http_request_duration_seconds`,
  `pos_sale_creates_total{result}`, `pos_payment_events_total{result}`, `pos_sales_paid_total`),
  route labels templated (never a resolved path), `/health`/`/ready`/`/version`/`/metrics`
  excluded from HTTP metrics, no tenant/sale/till/attendant/product id in any label.
- [x] Contract and cardinality-safety tests — `services/pos/tests/test_metrics.py` (6 tests):
  endpoint shape, operational-route exclusion, route templating vs. a real resolved path,
  unmatched-path bucketing, per-result sale-create/payment-event counts, and a direct
  no-ids-in-labels assertion. Verified 2026-09-30: `pytest -q` — 54 passed (full POS suite,
  including these 6).
- [x] Defense walkthrough written for live narration — `docs/g3-pos-walkthrough.md` (sale flow,
  idempotency/conflict handling, the replay-safe state machine, the reconcile scheduler, and
  `/metrics`).
- Grafana panels, alarms, and the ADOT scrape path for this endpoint are Emebet's
  (ADR 0010 R-4/R-5), not tracked here.
- No web frontend work is tracked at G3 — `services/web` is explicitly out of scope, see
  `services/web/README.md` and ADR 0010's Product decision on open question 1.

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
