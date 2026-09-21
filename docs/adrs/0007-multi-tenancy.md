# ADR 0007 — Multi-tenancy isolation

- **Status:** Accepted
- **Date:** 2026-09-21
- **DRI:** Product + POS — Alice Moraa (`@Moraaalice`)

## Context

TillFlow is multi-tenant: tills, attendants, roles, products, and sales must not leak across
tenants. POS owns this boundary for its own data; Payments/Commission enforce the same
`tenant_id` discipline on their side of the contract (see
`services/_shared/pos-payments-contract.md`).

## Decision

- **Isolation model:** single database, per-service schema (per ADR 0002), **row-level
  isolation by `tenant_id`** — every POS table (`tills`, `attendants`, `products`, `sales`,
  `sale_line_items`) carries a `tenant_id` foreign key. There is no separate schema or database
  per tenant; the capstone's tenant count doesn't justify the operational cost, and row-level
  isolation is what per-service least-privilege DB roles (ADR 0002) are designed around.
- **Path shape carries the tenant:** every tenant-scoped POS endpoint is nested under
  `/tenants/{tenant_id}/...` (e.g. `/tenants/{tenant_id}/sales`). There is no implicit "current
  tenant" derived from a session — the tenant is always explicit in the URL, and every handler
  re-validates that referenced child resources (`till_id`, `attendant_id`, `product_id` on a
  sale) actually belong to that `tenant_id` before using them. A request naming a real
  `till`/`attendant`/`product` from a *different* tenant is rejected with `400`, not silently
  redirected to the caller's own tenant — see `services/pos/tests/test_sale_create.py::
  test_sale_rejects_product_from_another_tenant`.
- **Roles:** tenant-scoped, two values for now — `OWNER` (configures the till, attendants,
  commission rates) and `ATTENDANT` (records sales). Enforced today only as a data field on
  `Attendant.role`; endpoint-level authorization (rejecting an `ATTENDANT` calling
  owner-only setup endpoints) is not yet wired — see Consequences.
- **Idempotency keys are tenant-scoped, not global:** `(tenant_id, idempotency_key)` is the
  actual uniqueness constraint on `sales`, not `idempotency_key` alone. Two different tenants
  are allowed to reuse the same client-generated key without colliding.
- **Validation boundary — server-priced sales:** the client sends `product_id` + `quantity`
  only; POS always computes `unit_price_minor` and the sale total from the tenant's own product
  catalog at creation time. A client can never supply its own price, which closes off a class
  of tenant-boundary and integrity bugs at once (a client can't under-price a sale, and it
  can't reference another tenant's pricing).

## Consequences

- AuthN/authZ (verifying the caller is actually a member of `tenant_id` with the right role) is
  **not implemented yet** — every endpoint currently trusts the `tenant_id` in the path. This is
  acceptable for the G2 product-correctness demo (sale/payment/commission invariants), but is a
  real gap before this could be called production-ready; tracked as a follow-up, not silently
  ignored (see `docs/production-readiness.md`).
- Every new POS table must remember to add `tenant_id` and use it in every query — there's no
  ORM-level automatic tenant filter (e.g. a session-scoped filter) yet. Reviewers should treat a
  new query without an explicit `tenant_id` check as a bug.
- Commission (reading "confirmed paid sales" for daily close) must also filter by `tenant_id`
  when it lands — flagged here so Payments/Commission don't have to rediscover this rule.

## Alternatives considered

- **Schema-per-tenant or database-per-tenant:** rejected — the operational and migration
  overhead isn't justified at this scale, and it would fight ADR 0002's per-service (not
  per-tenant) schema decision.
- **Implicit tenant from an auth token/session** instead of an explicit path segment: rejected
  for now since there's no auth layer yet; an explicit path also makes the contract easier for
  Payments/Commission/web to reason about and test without needing a real auth flow first.
