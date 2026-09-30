# Evidence: Payments + integrity

DRI: Mitingi Joy Chesang (`@chesangJ`). Area: Daraja STK/B2C, callbacks, payment and payout state,
idempotency, reconciliation and replay (`docs/ownership.md`). Code: `services/payments/`,
`services/commission/`, `services/_shared/mpesa/`.

This page is the evidence pack for the area, in the order the brief asks: decisions, commits and
PRs, tests, runtime proof, reproduction commands, then what is **not** claimed.
[`defence.md`](defence.md) has the design, trade-offs and failure behaviour for the live defence.

## 1. Decisions (ADRs)

| ADR | Decision | Status |
|---|---|---|
| [0004](../../docs/adrs/0004-mpesa-adapter.md) | One M-Pesa port; only Payments' adapter talks to Daraja; CI, tests and k6 use the FakeAdapter | Accepted (G2) |
| [0006](../../docs/adrs/0006-idempotency-replay.md) | Idempotency keys, the payment state machine, callback replay and reorder, timeout is `UNKNOWN` never a decline | Accepted (G2) |
| [0008](../../docs/adrs/0008-b2c-payouts.md) | B2C payouts: server-derived payout key, one live disbursement per payout, kill switch, no automatic resubmission | Accepted (G2) |
| [0009](../../docs/adrs/0009-payments-observability.md) | Metrics at `/metrics`, JSON logs with `trace_id`, invariants endpoint, k6 against the fake | Accepted (G3) |

Money rows of the [threat model](../../docs/threat-model.md) name the test that covers each control.

## 2. Commits and PRs

`git log --author="Mitingi Joy Chesang" -- services/payments services/commission services/_shared`
lists the commits. PRs, with the account that authored the commits:

| PR | What | Author |
|---|---|---|
| #11 | M-Pesa port and deterministic FakeAdapter | `@chesangJ` |
| #12, #13, #17 | ADR 0006, ADR 0008, acceptance of 0004/0006/0008 | `@chesangJ` |
| #15, #16 | Payments service and the first Commission worker (G2) | `@chesangJ` |
| #20, #23, #59 | ADR 0009, and its acceptance with the G3 sign-off | `@chesangJ` |
| #21, #22 | POS paid-sales endpoint for Commission; daily close and B2C disbursement over a payout ledger | `@chesangJ` |
| #27 | Close-to-payout evidence, run twice ([`../commission-payout/`](../commission-payout/)) | `@chesangJ` |
| #28, #29, #32, #34, #36 | Daraja sandbox B2C adapter, its infra switch, the caller-address header at the edge, the observed callback allowlist ([`../daraja-b2c-contract/`](../daraja-b2c-contract/)) | `@chesangJ` |
| #47, #49 | G4 drills, callback verdicts and `trace_id` on anomalies in the logs, deployed drill evidence | `@chesangJ` |
| #39 | G3 metrics, JSON logs, trace propagation, invariants endpoint, k6 scripts (ADR 0009 G3-1 to G3-4) | `@Moraaalice` |
| #52, #64 | Thread-safe FakeAdapter and no silent 502s (found by k6); `/_admin` and `/_fake` refused at the public edge on real-adapter builds | `@emebetgirmay` |
| #25, #56 | Commission image and PR checks; Commission e2e on the EAT business day | `@emebetgirmay` |

## 3. Tests

277 tests, no network. They run on SQLite with the standard library only, and again on
PostgreSQL 16 in CI (`postgres-tests`), each test in its own schema:

| Suite | Tests | Covers |
|---|---|---|
| `services/_shared/tests` | 60 | The port contract and every FakeAdapter scenario (STK and B2C), its thread safety, and a guard that nothing in `_shared` imports an HTTP client or carries a Safaricom URL or secret |
| `services/payments/tests` | 160 | `test_payments.py` (40) and `test_payouts.py` (34): idempotency, the state machines, replay and reorder, timeouts, reconcile, limits, kill switch. `test_daraja_sandbox.py` (18): the real adapter over a recorded transport. `test_two_tasks.py` (two services racing on one database), `test_restore_reconcile.py`, `test_invariants.py`, `test_metrics.py`, `test_trace_evidence.py`, `test_otlp.py`, `test_public_edge.py`, `test_unhandled_errors.py`, `test_http.py`, `test_config.py`, and the no-outbound guard |
| `services/commission/tests` | 57 | Commission maths and carry-forward, the daily close, disburse and reconcile against the real Payments service in process, a guard that Commission never imports M-Pesa code, and `test_end_to_end.py`: real POS + Commission + Payments over HTTP, run twice |

## 4. Runtime proof

| Gate | Proof | Where |
|---|---|---|
| G2 | Seven invariants against a running service: round trip, duplicate callback, replayed request, timeout is `UNKNOWN`, unverified code is `UNKNOWN`, payout with duplicate result, replayed payout | `checks.json` and the `payment-*`/`payout-*` files here |
| G2 | `close.py` then `disburse.py` against `POST /payouts`, then again: one payout, one debit | [`../commission-payout/checks.json`](../commission-payout/checks.json) (9 checks) |
| G2 | Daraja sandbox B2C through the deployed service: accepted requests, observed callback sources, no double pay on replay or a second key, `CONFIGURATION` result trips the kill switch | [`../daraja-b2c-contract/`](../daraja-b2c-contract/) |
| G3 | Payments metrics scraped into CloudWatch, the Payments dashboard, Payments alarms (`reconcile-stale`, `critical-anomaly` and `cpu-high` fired and recovered in Slack), k6 with invariants held (27,988 payments = 27,988 credits) | [`../reliability-ops/g3-evidence.md`](../reliability-ops/g3-evidence.md), [`k6-analysis.md`](../reliability-ops/k6-analysis.md), [`docs/g3-payments-walkthrough.md`](../../docs/g3-payments-walkthrough.md) |
| G4 | Uncertain payment, uncertain payout, callback replay and reorder against the **deployed** service, each step timed | [`g4/`](g4/): `checks.json` (3 drills, 23 checks) |

The G4 replay drill also paged: `payments-critical-anomaly` fired within about a minute and
recovered, while the invariant held (incident 3 in the G3 evidence).

### G2 files

| File | Shows |
|---|---|
| `health.json`, `ready.json`, `version.json` | Probes, same shape as `services/pos` |
| `payment-roundtrip-*.json` | Create, callback, `SUCCEEDED`, one ledger entry |
| `payment-replay-*.json` | A callback delivered three times (`applied`, `replay`, `replay`) and a replayed request (200): still one ledger entry |
| `payment-timeout-*.json` | No callback: the sweep moves it to `UNKNOWN`, never `DECLINED` |
| `payment-unverified-*.json` | Result code 1037 (unverified) resolves to `UNKNOWN`, not a terminal outcome |
| `payout-*.json` | A B2C payout with a duplicate result and a replayed request: one ledger entry |
| `checks.json` | Pass or fail for each invariant above; `collect.sh` exits non-zero if any fails |

### G4 drills

| Drill | Forced failure | Must hold |
|---|---|---|
| `uncertain-payment` | STK callback never arrives (254000000007); the initiate call itself times out (254000000012) | Sweep moves it to `UNKNOWN`, never `DECLINED`; same-key retries while pending, while `UNKNOWN` and after recovery return the original (the fake refuses a second initiate for a key); reconcile by status query ends `SUCCEEDED` with one ledger entry |
| `uncertain-payout` | B2C result never arrives (254000000107) | `UNKNOWN`, never `FAILED`; Commission's same-key rerun returns the original, a second key gets `409 payout_already_requested`; reconcile ends `SUCCEEDED`, one ledger entry |
| `callback-replay` | Same callback 3x (254000000009, 254000000109); success then failure (254000000010, 254000000110); failure then success (254000000011) | Verdicts `applied, replay, replay` and `applied, illegal_transition_logged`; one ledger effect; a terminal state never flips; each delivery ran under its own trace id (`trace_ids` in `g4/checks.json`) |

Each step records `wall_s` (real time) and `provider_s` (the service's manual provider clock,
advanced past the 90 s callback deadline and 120 s reconcile SLA without waiting). Deployed run
2026-09-29: wall totals 17.3 s, 12.2 s and 101.6 s. The service answered each request in about
0.5 s; the spikes are the drill machine's network (single TCP connects of about 19 s, measured with
`curl -w %{time_connect}`), so read `wall_s` as an upper bound. `provider_s` is 230 s for both
uncertain drills: 100 s past the callback deadline, then 130 s past the reconcile SLA.

A trace explains a duplicate on its own: every delivery logs `{"event": "callback", "result":
"replay"}` under the delivery's `trace_id`, and anomalies carry the same `trace_id`
(`services/payments/tests/test_trace_evidence.py`):

```bash
aws logs filter-log-events --log-group-name /devops-g9/payments --filter-pattern '"<trace_id>"'
```

### One trace from sale to callback

One `traceparent` sent on a sale's POS calls is followed end to end, in logs and as spans.

| File | Shows |
|---|---|
| [`trace/sale-to-callback.json`](trace/sale-to-callback.json) (5 checks) | Filtering both services' logs by the sale's trace id returns POS's sale and payment-request, Payments' create and `PENDING`, the callback's `SUCCEEDED`, and POS's reconcile |
| [`trace/collector-spans.json`](trace/collector-spans.json) (3 checks) | The span tree an ADOT collector v0.43.1 (the sidecar's version) received: Payments' `POST /payments` under POS's `payment-request`, and the callback under that create span |

```
pos POST /tenants/{tenant_id}/sales
pos POST /tenants/{tenant_id}/sales/{sale_id}/payment-request
  payments POST /payments
    payments payment PENDING -> SUCCEEDED        <- the provider's callback
pos POST /tenants/{tenant_id}/sales/{sale_id}/payment-reconcile
  payments GET /payments/{id}
```

The provider's callback is a separate request with its own trace id, so Payments stores the
creating trace and span id on the payment; when a later request (callback, sweep, reconcile) moves
the record it logs `origin_trace_id` and emits a span into the sale's trace as a child of the
create. Spans go to the ADOT sidecar as OTLP/HTTP JSON from the standard library, loopback only,
off the request path (`core/otlp.py`, `tests/test_otlp.py`). Commission sends one run trace id
from `close.py` to POS and from `disburse.py` to Payments.

### PostgreSQL (ADR 0002)

Payments and Commission run on PostgreSQL 16, the engine RDS runs. [`postgres/`](postgres/) holds
runs against the **shipped Payments image** on PostgreSQL:

| File | Shows |
|---|---|
| `postgres/g2-checks.json`, `postgres/g4-checks.json` | The G2 pack (7 checks) and the G4 drills (23 checks) pass unchanged on PostgreSQL |
| `postgres/restart-checks.json` (7 checks) | A **new task on the same database** still has the succeeded payment and payout with their ledger entries, returns the original for the old idempotency keys, keeps the kill switch on, and moves the in-flight payment to `UNKNOWN`. On SQLite every one of these was lost on redeploy |

Tests behind it: the whole suite on both backends; `test_two_tasks.py`, two services racing the
same create and the same callback on one database (one payment, one ledger entry; it fails if the
writer lock is removed); `test_restore_reconcile.py`, the runbook's restore step 5: work that
completed after the restore point is unfinished in the restored data and is recovered by asking
the provider, with one ledger entry and nothing sent twice.

## 5. Reproduce

Every command below was run on 2026-09-30 from a fresh clone of `main` and passed.
Python 3.12, no credentials, nothing reaches Safaricom.

```bash
# Tests (277). Commission's two end-to-end tests need POS's packages; without them they skip.
(cd services/_shared    && python3 -m unittest discover -s tests -t .)   # 60
(cd services/payments   && python3 -m unittest discover -s tests)        # 160
python3 -m venv .venv && .venv/bin/pip install -r services/pos/requirements.txt
(cd services/commission && ../../.venv/bin/python -m unittest discover -s tests)   # 57

# Two local services for the runtime packs.
(cd services/payments && DATABASE_URL=sqlite:////tmp/ev-payments.db python3 app.py) &
(cd services/pos && DATABASE_URL=sqlite:////tmp/ev-pos.db PAYMENTS_BASE_URL=http://127.0.0.1:8080 \
  ../../.venv/bin/uvicorn app.main:app --port 8081) &

PAYMENTS_URL=http://127.0.0.1:8080 ./evidence/payments-integrity/collect.sh          # G2, 7 checks
PAYMENTS_DB=/tmp/ev-payments.db ./evidence/commission-payout/collect.sh              # G2, 9 checks, waits 61 s
PAYMENTS_URL=http://127.0.0.1:8080 python3 evidence/payments-integrity/g4/drills.py  # G4, 23 checks
# PostgreSQL: the same suites, and the restart check around replacing the task.
pip install -r services/payments/requirements.txt
docker run -d -e POSTGRES_PASSWORD=test -e POSTGRES_DB=tillflow -p 127.0.0.1:55440:5432 postgres:16
export TEST_POSTGRES_URL=postgresql://postgres:test@127.0.0.1:55440/tillflow
(cd services/payments   && python3 -m unittest discover -s tests)
(cd services/commission && python3 -m unittest discover -s tests)
python3 evidence/payments-integrity/postgres/restart.py before   # then replace the task, then: after
# Trace: start the two services with their output redirected to files, then
LOGS="/tmp/tr-pos.log /tmp/tr-payments.log" ./evidence/payments-integrity/trace/collect.sh   # 5 checks
```

The packs overwrite the JSON next to them, so run them in a scratch clone to keep the committed
run. Against the deployed service, set `PAYMENTS_URL` to the API Gateway endpoint
(`terraform output -raw api_gateway_url` in `infra/envs/sandbox`); the drills need the FakeAdapter, which is what the
sandbox runs.

## 6. Not claimed

- **A `SUCCEEDED` payout from the real Daraja sandbox.** Five runs proved the request, the callback
  path and the fail-safes; runs 3 and 5 ended `CONFIGURATION` (see the contract-test README). STK
  through the real adapter, the `QueueTimeOutURL` handler and the asynchronous Transaction Status
  query are not built (ADR 0008).
- **Running on RDS in the sandbox.** The code, images and CI are ready and proven on a local
  PostgreSQL 16; the deployed Payments still uses SQLite until `payments_database = "rds"` is
  flipped, which needs the RDS bootstrap script run first. The timed point-in-time restore (RPO,
  RTO) on RDS is Platform's drill; the reconcile step of it is proven here as a test.
- **Capacity.** One writer at a time, by design, on both backends. k6 proves correctness under
  concurrency, not production sizing; the capacity run on RDS is not done (ADR 0009 G3-5).
- **Commission in the sandbox.** It has an image and CI and runs on PostgreSQL, but is not
  deployed: the scheduled task is Platform's to add.
- **The X-Ray screenshot itself.** Spans are exported and the tree is proven against the same
  ADOT collector version the sidecar runs, locally. The waterfall captured from X-Ray needs the
  deployed services and is Platform's to capture.
- **The exact Daraja result code** is logged on every callback line (`code`) but not returned by
  `GET /payouts`.
