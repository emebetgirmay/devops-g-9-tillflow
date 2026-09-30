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
  against the live API Gateway URL, most recently on 2026-09-30: `sale-demo-create.json` (sale
  created, `READY_FOR_PAYMENT`) -> `sale-demo-payment-request.json` (STK requested) ->
  `sale-demo-fake-deliver.json` (Payments' deterministic fake STK callback, standing in for
  Daraja) -> `sale-demo-reconcile-1.json` (`PAID` on the first poll) -> `sale-demo-final.json`.
  `sale-demo-reconcile-replay.json` proves a repeat reconcile is a no-op (`applied: false`). (First
  run for record was 2026-09-29; re-run 2026-09-30 while verifying the G3 sign-off below —
  each run overwrites these files with its own sale, so the evidence always reflects the latest.)

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
- [x] Verified live against the deployed sandbox on 2026-09-30 (re-running the sale demo above
  while confirming this section): the POS Grafana dashboard's route/result-labelled panels
  (`Requests by route and status`, `Sale creates by result`, `Payment events applied to sales`)
  render real, correctly-labelled data end to end. One known cold-start gap found and confirmed
  with `aws cloudwatch get-metric-statistics` at 60s resolution: a metric label combination's
  first-ever occurrence in a task's lifetime is silently dropped by ADOT's cumulative-to-delta
  conversion (no prior sample to diff against); only its second-or-later occurrence shows up.
  `pos_sales_paid_total` (unlabelled, so its one series has existed since task start) is
  unaffected. Not a broken pipeline — see [scar log](../../docs/scar-log.md), 2026-09-30
  "POS / observability".
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

## G4 — Recover

- [x] Failure + recovery drill against the deployed sandbox — `evidence/product-pos/g4-recover-stuck-sale.sh`.
  Proves the real gap in the POS ↔ Payments contract (Payments has no push callback, so a sale can
  settle at Payments while POS's own record never finds out until something polls) by forcing it
  open, then recovering from it correctly. Run for real on 2026-09-30, all 5 checks passed
  (`g4-recover-checks.json`):
  1. `sale_stuck_at_payment_requested_before_reconcile` — Payments resolved the STK to `SUCCEEDED`
     (`g4-recover-fake-deliver.json`) while POS's own record was still `PAYMENT_REQUESTED`
     (`g4-recover-stuck-state.json`) — the failure, made real, not assumed.
  2. `unsafe_retry_payment_request_is_refused` — retrying `payment-request` on the stuck sale, as
     if re-sending the STK push were a fix, is refused with 409 (`g4-recover-unsafe-retry.json`),
     not silently re-sent.
  3. `reconcile_recovers_sale_to_paid` / `reconcile_reports_applied_true_on_recovery` — the correct
     recovery, `payment-reconcile`, applies the already-known outcome and reaches `PAID`
     (`g4-recover-reconcile.json`) — no new charge, no new STK push, POS just catches up.
  4. `repeat_reconcile_is_a_no_op` — the recovery action is itself safe to repeat
     (`g4-recover-reconcile-replay.json`, `applied: false`).
- Restore drill and RDS backups are blocked on RDS provisioning (ADR 0002) — Platform's, not POS's.

### Reproduction

```bash
aws sso login --profile g9   # or: aws login --profile g9
AWS_PROFILE=g9 ./evidence/product-pos/g4-recover-stuck-sale.sh
```

## Later
- Live trace (sale -> payment-request -> payment-event) once ADOT/Payments are wired end to end.
- Cross-review sign-off from Payments DRI on `services/_shared/pos-payments-contract.md`.
