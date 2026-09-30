# Payments API (G2)

Group: devops-g9 · TillFlow · DRI: Mitingi Joy Chesang (`@chesangJ`)

Idempotent M-Pesa STK charges and B2C payouts over the ports in `services/_shared/mpesa`.
Rules come from [ADR 0004](../../docs/adrs/0004-mpesa-adapter.md) (boundary),
[ADR 0006](../../docs/adrs/0006-idempotency-replay.md) (charges, callbacks, replay) and
[ADR 0008](../../docs/adrs/0008-b2c-payouts.md) (payouts, still Proposed).

**Commission never calls Daraja; it calls this service's `/payouts` endpoint only** (ADR 0004).

## What this build does not do

- **Daraja sandbox is B2C only.** `MPESA_ADAPTER=daraja_sandbox` selects
  `core/daraja_sandbox.py` for payouts in the deployed sandbox. It refuses to start unless every
  `MPESA_*` setting below is present and the base URL is an https `sandbox.` host, and it never
  falls back to the fake. STK charges through it are declined at initiation (not built), the
  `QueueTimeOutURL` notification is not handled (the sweep covers it), and the Transaction Status
  query always answers `UNKNOWN`, so an unresolved payout ends in `NEEDS_REVIEW` for an operator.
  CI, tests and k6 use the FakeAdapter only. That file is the only one here allowed an HTTP client,
  and no Safaricom URL or secret lives in code; `tests/test_no_outbound_guard.py` fails if that
  changes.
- **Two storage backends (ADR 0002).** SQLite for local runs and unit tests; PostgreSQL when
  `DATABASE_URL` is `postgresql://…` (the `devops-g9/db/payments` secret on RDS). `core/db.py`
  gives both the same interface, and the same rule: one write transaction at a time
  (`BEGIN IMMEDIATE` on SQLite, an advisory lock on PostgreSQL), so every invariant holds on one
  task or several. The driver (`requirements.txt`, psycopg) is imported only for PostgreSQL. CI
  runs the whole suite on both.
- The fake keeps its state in memory, so `reconcile.py` only resolves references issued by the
  same process; use `--service-url` against the running service.

## Endpoints (JSON)

| Method and path | Purpose |
|---|---|
| `GET /health`, `/ready`, `/version` | Same shape as `services/pos`. `/ready` checks the database. |
| `POST /payments` | Start a charge. Header `Idempotency-Key` required. Body `tenant_id`, `msisdn`, `amount` (integer minor units), `account_reference` (at most 12 chars), optional `sale_id`. |
| `GET /payments/{id}` | State by `payment_id` or `checkout_request_id`. |
| `POST /payments/daraja/callback` | STK result callback. |
| `POST /payments/{id}/reconcile` | Query the provider for a payment stuck in `UNKNOWN` or `NEEDS_REVIEW`. |
| `POST /payouts` | Request a B2C payout. Header `Idempotency-Key` required. Body `tenant_id`, `attendant_id`, `payout_period`, `msisdn`, `amount` (minor units). |
| `GET /payouts/{id}` | State by `disbursement_id`, originator id or conversation id. |
| `POST /payments/daraja/b2c-callback` | B2C result callback. |
| `POST /payouts/{id}/reconcile` | Query the provider for a payout stuck in `UNKNOWN` or `NEEDS_REVIEW`. |
| `POST /_admin/sweep` | Run the reconcile pass (also `python3 reconcile.py`). |
| `GET /_admin/invariants` | Fake-adapter builds only (`404` otherwise) — the counts a k6 run asserts stay at zero throughout (ADR 0009 section 6 / `G3-3`). |
| `POST /_fake/advance`, `/_fake/deliver-callbacks`, `/_fake/script-result-code` | Drive the FakeAdapter for tests and k6. `404` under the sandbox adapter. |
| `GET /metrics` | Prometheus text, scraped by the ADOT sidecar — not meant to be internet-reachable (see Observability below). |

### Idempotency contract

- **201** the first time; **200 with `Idempotent-Replayed: true`** and the original body when the
  same `(tenant_id, Idempotency-Key)` is sent again with the same payload. **409**
  `idempotency_key_payload_mismatch` for a different payload, **409** `idempotency_in_flight` (with
  `Retry-After`) for a concurrent duplicate. Keys are 16 to 64 characters of `[A-Za-z0-9_-]`.
- A payment for a `sale_id` is refused (**409** `sale_already_has_live_payment`) while another
  payment for that sale is not `DECLINED` or `EXPIRED`.
- `payout_key` is derived here from tenant, attendant and period; a caller cannot send one. At
  most one disbursement per payout key is not `FAILED`, and the provider identifier
  (`OriginatorConversationID`) is derived from the disbursement and stored before the provider call.
- Payout amounts must be whole shillings (multiples of 100 minor units), at least KSh 10, and at
  most `PAYOUT_MAX_MINOR` (default and hard cap KSh 250,000); otherwise **422**.

### Callbacks (ADR 0006 section 4)

Source-IP allowlist (`CALLBACK_ALLOWED_IPS`) first, then the adapter's authenticity check, then one
transaction that applies the outcome. Valid, replayed and unmatched callbacks get **200**; a failed
source or authenticity check gets **403**; an authentic but malformed body gets **400**. A replay
produces no second ledger entry, event or notification. A success is confirmed with a status query
before it is applied (`CONFIRM_SUCCESS_WITH_QUERY`, default on); an amount mismatch goes to
`NEEDS_REVIEW`. The gateway discards results sent while this service is down, so the reconcile
pass is the recovery path. The allowlist checks the socket peer only; where it is enforced behind a
proxy is an open question in ADR 0006.

### States

Payments: `CREATED`, `PENDING`, `UNKNOWN`, `NEEDS_REVIEW`, `SUCCEEDED`, `DECLINED`, `EXPIRED`.
Payouts: the same with `FAILED` in place of `DECLINED` and `EXPIRED`. Terminal states are immutable;
`core/states.py` holds the only legal transitions and a contradicting result is logged as an
anomaly, never applied. A timeout is `UNKNOWN`, never a decline, and is never retried. After
`RECONCILE_WINDOW_SECONDS` (24 h) an unresolved item becomes `NEEDS_REVIEW`, not failed. Only result
codes verified against Daraja documentation are terminal; every other code is `UNKNOWN`.

### Payouts kill switch

A `CONFIGURATION` failure (codes 21, 2001, 2028, 8006) or insufficient funds sets the
`payouts_enabled` flag off, so `POST /payouts` answers **503** `payouts_disabled` until an operator
sets `flags.payouts_enabled` back to 1 in the database. `PAYOUTS_ENABLED=false` starts it off.

## Observability (ADR 0009, G3)

Three pieces, all stdlib-only (no metrics/tracing client library — see the no-outbound-client
guard below):

- **`GET /metrics`** (`core/metrics.py`) — the full `payments_*` catalogue from ADR 0009 section
  2: HTTP RED (`payments_http_requests_total`/`_duration_seconds`, route *templates* only — never
  a raw path with an id in it), create outcomes, every state transition, callback outcomes,
  adapter call health/latency, reconcile run outcomes, and anomalies. Four of the metrics
  (`payments_records`, `payments_oldest_age_seconds`, `payments_reconcile_last_success_timestamp_seconds`,
  `payments_payouts_enabled`) are **not** in-process counters at all — they're computed fresh from
  the database on every scrape, so they're correct immediately after a restart and identical
  across tasks (aggregate with `max` in Grafana, never `sum`). No tenant, msisdn, payment or
  disbursement id is ever a label — that's an unbounded-cardinality mistake waiting to happen;
  those stay in logs only.
- **Structured JSON logs + trace propagation** (`core/jsonlog.py`, `core/tracing.py`) — one line
  per request and one per state change, both carrying `trace_id`. A trace id comes from an
  incoming W3C `traceparent` header (Commission's `ledger/tracing.py` sends one) or is generated
  fresh, and is echoed back as `X-Trace-Id`, so one Commission disburse run can be followed
  through into Payments' own log lines. Distinct from `Store.anomaly()`'s existing stderr lines,
  which are unchanged.
- **`GET /_admin/invariants`** and **`k6/`** — see the endpoints table and "Run and test" below.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `PORT` | `8080` | Listen port |
| `DATABASE_URL` | `sqlite:///data/payments.db` (under this folder) | `sqlite:///<path>`, or `postgresql://…` on RDS |
| `DB_POOL_SIZE` | `5` | PostgreSQL connections kept by this task |
| `MPESA_ADAPTER` | `fake` | `fake`, or `daraja_sandbox` (deployed sandbox, B2C only; forces the system clock) |
| `MPESA_BASE_URL` | | `daraja_sandbox`: https `sandbox.` provider host |
| `MPESA_CONSUMER_KEY`, `MPESA_CONSUMER_SECRET` | | `daraja_sandbox`: app credentials, from `devops-g9/daraja` |
| `MPESA_B2C_SHORTCODE`, `MPESA_B2C_INITIATOR_NAME` | | `daraja_sandbox`: B2C short code and API initiator |
| `MPESA_B2C_SECURITY_CREDENTIAL` | | `daraja_sandbox`: initiator password already encrypted with the sandbox certificate |
| `MPESA_CALLBACK_BASE_URL` | | `daraja_sandbox`: public https base; results go to `/payments/daraja/b2c-callback` |
| `FAKE_CLOCK` | `manual` | `manual` (advance with `/_fake/advance`) or `system` |
| `CALLBACK_ALLOWED_IPS` | `127.0.0.1,::1` | Comma-separated source allowlist |
| `CALLBACK_SOURCE_HEADER` | empty | Header our edge overwrites with the caller's address (API Gateway maps `$context.identity.sourceIp` into `x-tillflow-source-ip`). The callback allowlist checks it; empty checks the socket peer |
| `CONFIRM_SUCCESS_WITH_QUERY` | `true` | Confirm a success callback with a status query |
| `RECONCILE_SLA_SECONDS` | `120` | Minimum time in `UNKNOWN` before the reconcile pass queries it |
| `CALLBACK_DEADLINE_SECONDS` | `90` | `PENDING` to `UNKNOWN` |
| `CREATED_SWEEP_SECONDS` | `120` | A `CREATED` row this old is treated as a lost attempt |
| `RECONCILE_WINDOW_SECONDS` | `86400` | Then `NEEDS_REVIEW` |
| `PAYOUT_MAX_MINOR` | `25000000` | Per-payout ceiling (never above the provider maximum) |
| `PAYOUTS_ENABLED` | `true` | Initial kill-switch state |
| `COMMIT_SHA`, `IMAGE_DIGEST` | `local`, `unknown` | Reported by `/version` |

## Run and test

```bash
python3 app.py                                              # from this folder
python3 -m unittest discover -s tests                       # from this folder
docker build -f services/payments/Dockerfile -t local/payments:pr .   # from the repo root
```

Evidence for the invariants: `evidence/payments-integrity/collect.sh`.

### k6 (ADR 0009 section 6, `G3-4`)

Against the FakeAdapter only — never the sandbox adapter. Run the server with `FAKE_CLOCK=system`
(real elapsed time, so timeouts genuinely elapse instead of needing manual `/_fake/advance`
calls):

```bash
FAKE_CLOCK=system python3 app.py &
PAYMENTS_URL=http://127.0.0.1:8080 k6 run k6/capacity.js      # smoke/stepped/spike/soak envelope
PAYMENTS_URL=http://127.0.0.1:8080 k6 run k6/correctness.js   # replay/duplicate/timeout/kill-switch
```

`k6/capacity.js` defaults its soak stage to 20s so the script itself iterates fast; the real
evidence run is `SOAK_DURATION=15m k6 run k6/capacity.js` (ADR 0009 section 6's "at least 15
minutes"). `k6/correctness.js` needs a short `RECONCILE_SLA_SECONDS`/`CALLBACK_DEADLINE_SECONDS`
(e.g. `5`) on the server for its timeout-then-late-success case to actually resolve within the
script's ~35 s run — production keeps its real defaults; this is a k6-harness-only override.
Both scripts end by asserting `GET /_admin/invariants` never went bad, which is the real
pass/fail signal, not just individual response codes.

**Capacity caveat (ADR 0009 section 6):** SQLite serialises writers, so these numbers prove
correctness under concurrency, not capacity — real sizing evidence needs the Postgres backend
(blocked on RDS, ADR 0002).
