# Payments API (G2)

Group: devops-g9 · TillFlow · DRI: Mitingi Joy Chesang (`@chesangJ`)

Idempotent M-Pesa STK charges and B2C payouts over the ports in `services/_shared/mpesa`.
Rules come from [ADR 0004](../../docs/adrs/0004-mpesa-adapter.md) (boundary),
[ADR 0006](../../docs/adrs/0006-idempotency-replay.md) (charges, callbacks, replay) and
[ADR 0008](../../docs/adrs/0008-b2c-payouts.md) (payouts, still Proposed).

**Commission never calls Daraja; it calls this service's `/payouts` endpoint only** (ADR 0004).

## What this build does not do

- **No real Daraja adapter.** Only the FakeAdapter is wired. `MPESA_ADAPTER=daraja_sandbox` (or
  any other value) makes the service exit with code 2 at startup, with or without credentials, and
  it never falls back to the fake. The sandbox adapter belongs to the deployed environment with
  Platform-managed secrets. Nothing in this folder holds a Safaricom URL, an HTTP client or a
  secret; `tests/test_no_outbound_guard.py` fails if that changes.
- **No Postgres yet.** SQLite is the storage. A `postgres://` `DATABASE_URL` is refused at startup
  until RDS lands (ADR 0002).
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
| `POST /_fake/advance`, `/_fake/deliver-callbacks`, `/_fake/script-result-code` | Drive the FakeAdapter for tests and k6. This build has no other adapter. |

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

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `PORT` | `8080` | Listen port |
| `DATABASE_URL` | `sqlite:///data/payments.db` (under this folder) | `sqlite:///<path>` only |
| `MPESA_ADAPTER` | `fake` | Only `fake` is accepted |
| `FAKE_CLOCK` | `manual` | `manual` (advance with `/_fake/advance`) or `system` |
| `CALLBACK_ALLOWED_IPS` | `127.0.0.1,::1` | Comma-separated source allowlist |
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
