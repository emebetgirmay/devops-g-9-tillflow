# Threat model — TillFlow (G0 draft; payments rows updated in G2)

**Method:** lightweight STRIDE on trust boundaries · **Owner:** Product + Payments DRIs co-author; Platform implements controls · **Sandbox only:** Daraja 3.0; no real money or customer PII in git.

## Assets

| Asset | Why it matters |
|---|---|
| Sale / payment / payout ledger | Double-charge or double-pay = product failure |
| Daraja credentials & access tokens | Account takeover of till / B2C |
| Tenant & attendant identity / roles | Cross-tenant data leak |
| Terraform state & pipeline OIDC roles | Infra compromise |
| Slack webhook | Alert spam / phishing via ops channel |

## Trust boundaries

1. Internet → API Gateway / web
2. Gateway → VPC Link → private ALB → ECS
3. Payments → Daraja sandbox HTTPS (not exercised yet: G2 wires the FakeAdapter only)
4. CI (GitHub OIDC / CodeBuild) → AWS deploy roles
5. Humans → Secrets Manager / GitHub Environments

## STRIDE summary

| Threat | Example | Mitigation (G0 intent) |
|---|---|---|
| **S**poofing | Forged STK callback | Validate Daraja callback authenticity; idempotent state machine; never trust body alone for money transitions |
| **T**ampering | Replay callback / duplicate B2C | Idempotency keys; single legal transition; payout ledger unique constraint; DLQ for poison messages |
| **R**epudiation | “We never got paid” | Immutable-ish ledger rows; structured JSON logs with `trace_id`; S3 evidence |
| **I**nformation disclosure | Cross-tenant reads; secrets in logs | Tenant-scoped authZ; per-service DB roles; no secrets in git/TF state outputs/build logs; block-public S3 |
| **D**enial of service | Callback flood; cache outage | Rate limits at edge; SQS buffering; degrade gracefully; alerts |
| **E**levation of privilege | Commission calling Daraja with broad creds | Commission → Payments API only; least-privilege IAM task roles; separate secrets |

## Payments controls as built (G2)

What the Payments service and Commission worker actually do, for the money-path threats above
([ADR 0004](adrs/0004-mpesa-adapter.md), [ADR 0006](adrs/0006-idempotency-replay.md),
[ADR 0008](adrs/0008-b2c-payouts.md)). Tests are in `services/payments/tests`,
`services/commission/tests` and `services/_shared/tests`; run-through evidence is in
`evidence/payments-integrity/`. All of it runs against the FakeAdapter; nothing has touched Daraja.

| Threat | Control | Proof |
|---|---|---|
| Forged or spoofed callback (STK and B2C) | The handler checks the source-IP allowlist first (the control Daraja documents), then the adapter's authenticity check, then an amount check; an STK success is confirmed with a status query before it is applied (default on) | `test_a_callback_from_a_source_outside_the_allowlist_is_rejected`, `test_a_tampered_or_unsigned_callback_is_rejected_and_changes_nothing`, `test_a_success_the_status_query_contradicts_is_not_applied`, `test_an_amount_mismatch_goes_to_review_and_is_not_credited` |
| Replayed callback, duplicate charge | Database constraints, not only code: idempotency key primary key with a request fingerprint, one ledger entry per provider reference, one outbox event per record, one live payment per sale | `test_replays_are_2xx_with_no_second_ledger_entry_event_or_notification`, `test_concurrent_duplicate_requests_produce_one_charge` |
| Duplicate B2C, double pay | Server-derived payout key, one live disbursement per payout (partial unique index), a deterministic `OriginatorConversationID` stored before the provider call (the provider rejects a repeat), no automatic resubmission | `test_a_second_key_for_the_same_payout_cannot_pay_twice`, `test_running_twice_yields_exactly_one_payout_per_entry` |
| Timeout mistaken for a decline | A timeout is `UNKNOWN`, never terminal and never retried; reconcile with backoff; after 24 h `NEEDS_REVIEW`, never auto-failed; only verified result codes are terminal | `test_no_callback_becomes_unknown_never_a_decline_and_is_escalated_not_failed`, `test_only_verified_codes_are_in_the_table` |
| Contradicting late result | Terminal states are immutable; a contradiction is logged as a critical anomaly and never applied | `test_success_then_a_stale_failure_stays_succeeded`, `test_failure_then_a_success_stays_declined_and_is_not_credited` |
| Commission holding broad Daraja access | Commission imports no M-Pesa code and calls `POST /payouts` only; Payments has no outbound HTTP client; the service refuses any adapter but the fake | `services/commission/tests/test_no_daraja_guard.py`, `services/payments/tests/test_no_outbound_guard.py`, `StartupRefusalTest` |
| Bad configuration or locked credentials halting or repeating payouts | A configuration failure (codes 21, 2001, 2028, 8006) or insufficient funds trips the payouts kill switch; nothing retries in a loop | `test_configuration_errors_trip_the_kill_switch`, `test_insufficient_funds_fails_the_payout_and_pauses_the_run` |
| Personal data in storage or logs | Callback audit keeps a hash and summary, not the body; phone numbers are masked in API responses; logs carry ids only | `provider_callbacks` schema in `services/payments/core/store.py` |

## Explicit non-goals (sandbox)

- PCI card data (out of scope — M-Pesa only)
- Production Safaricom credentials
- Real phone numbers / KYC data in fixtures

## Abuse cases to prove later (G4)

1. Timeout then reconcile — no second charge (proven against the FakeAdapter in G2; the deployed-stack run is still G4)
2. Reordered/duplicate callbacks — one ledger effect (proven against the FakeAdapter in G2; the deployed-stack run is still G4)
3. Broken release — smoke fail → ECS rollback
4. Restore + reconcile provider refs before “recovered”

## Open risks

| Risk | Owner | Status |
|---|---|---|
| Callback authenticity in production | Payments (enforcement point: Platform) | Partly mitigated in G2. Daraja documents a source-IP allowlist of its gateway addresses and no callback signature. The Payments handler enforces the allowlist and adds a confirming status query. Still open: where the allowlist is enforced behind the API gateway ([ADR 0006](adrs/0006-idempotency-replay.md) question 10, `@emebetgirmay`), whether to add an unguessable callback path, and confirming the address list with Safaricom |
| Callbacks discarded while the endpoint is down | Payments, Reliability | Daraja documents that results sent while the callback server is unavailable are discarded, not queued. Mitigation is the reconcile pass; still open: a scheduled reconcile job and a highly available callback endpoint ([ADR 0006](adrs/0006-idempotency-replay.md) questions 9 and 10, `@emebetgirmay`) |
| B2C cannot be reversed through the API and needs no provider approval | Payments, Product | Documented by Daraja. Mitigated before money leaves: recipient and amount fixed at planning, ceilings, kill switch, no resubmission. Open: the Commission payout ledger and recipient snapshot are not built yet, and the ceiling values are undecided ([ADR 0008](adrs/0008-b2c-payouts.md) questions 3 and 9) |
| A locked B2C API user halts every payout | Payments, Platform | Mitigated by the kill switch (no retry storm). Open: a separate API user and secret per Daraja role, and a runbook entry ([ADR 0006](adrs/0006-idempotency-replay.md) question 12, [ADR 0008](adrs/0008-b2c-payouts.md) questions 6 and 8) |
| Daraja result codes not yet verified | Payments | Only STK codes 0 and 1032 and the documented B2C codes are terminal; everything else resolves to `UNKNOWN`. Open: the sandbox contract test run by the deployed adapter, which is not built |
| Test and admin endpoints are unauthenticated | Payments | `/_fake/*` and `/_admin/sweep` exist in the fake-only build. Acceptable there; they must be removed or authenticated before any deployed use |
| OIDC trust conditions too broad | Platform | TBD by G1 |
| Slack webhook exfiltration | Reliability | Secrets Manager only |
