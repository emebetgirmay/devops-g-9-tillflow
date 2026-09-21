# POS API

Group: devops-g9 · TillFlow · DRI: Alice Moraa (`@Moraaalice`)

FastAPI service owning tenants, tills, attendants, products, and sales. Computes sale totals
from the tenant's own product catalog (never a client-supplied price), enforces idempotent sale
creation, and owns the sale state machine up to and through payment settlement. See
`services/_shared/pos-payments-contract.md` for the exact interface with Payments, and
`docs/adrs/0007-multi-tenancy.md` for the tenant-isolation model.

## Product flow

```
CREATE SALE -> SALE ID -> TOTAL -> READY_FOR_PAYMENT -> (Payments) -> PAID
```

## Local dev

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/uvicorn app.main:app --reload --port 8080
```

Defaults to a SQLite file at `/tmp/pos.db` (no external DB needed). Set `DATABASE_URL` to a
`postgresql+psycopg://...` DSN to run against Postgres — the schema uses no SQLite/Postgres
-specific types, so nothing else changes when Platform wires up the real RDS instance and
injects the connection string.

## Tests

```bash
.venv/bin/pytest --cov=app --cov-report=term-missing
```

Covers: tenant-scoped validation, idempotent sale creation (including racing retries and
tenant-scoping of the idempotency key), server-computed totals, the sale state machine, and —
most importantly — the payment-event invariants: replayed and reordered callbacks each produce
exactly one legal transition and one ledger effect, never more.

## Build / push (G1 golden path, unchanged)

From repo root:

```bash
./scripts/build-push-pos.sh bootstrap
```

## Endpoints

- `POST /tenants`, `POST /tenants/{id}/tills`, `POST /tenants/{id}/attendants`,
  `POST /tenants/{id}/products` — tenant setup.
- `POST /tenants/{id}/sales` (requires `Idempotency-Key` header) — create a sale from
  `till_id`, `attendant_id`, and `line_items: [{product_id, quantity}]`.
- `GET /tenants/{id}/sales/{sale_id}`
- `POST /tenants/{id}/sales/{sale_id}/payment-request` — asks Payments for an STK push.
- `POST /internal/sales/{sale_id}/payment-events` — internal callback Payments uses to report
  PAID/PAYMENT_FAILED (not exposed through API Gateway).
- `GET /health`, `GET /ready`, `GET /version` — unchanged from the G1 stub.

## Known simplifications (tracked, not accidental)

- No auth/authz yet — every tenant-scoped endpoint trusts the `tenant_id` in the path. See
  ADR 0007 Consequences.
- Schema is created via `Base.metadata.create_all()` at startup rather than Alembic migrations —
  fine for the capstone's timeline; would need real migrations before any production use.
