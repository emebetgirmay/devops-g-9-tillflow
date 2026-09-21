# Architecture decision records

One file per decision, numbered in order: `NNNN-short-name.md`. Each ADR has a status, a date, a DRI
(one owner, matching `CODEOWNERS`), and the sections Context, Decision, Consequences and
Alternatives considered. ADRs link to the ones they depend on. Open questions stay listed in the
ADR, each with an owner, and do not block acceptance of the decision itself.

**Status values:** `Proposed` (written, not signed off) and `Accepted (gate)` (owner set it and the
reviewers approved the PR). A status change is made by the ADR's owner in its own file, and this
index mirrors the files.

| ADR | Topic | Owner | Status |
|---|---|---|---|
| [0001](0001-aws-region.md) | AWS region | `@emebetgirmay` | Accepted (G0) |
| [0002](0002-rds-postgresql.md) | RDS PostgreSQL baseline | `@emebetgirmay` | Proposed (G0) |
| [0003](0003-s3-buckets.md) | S3 buckets | `@emebetgirmay` | Proposed (G0) |
| [0004](0004-mpesa-adapter.md) | M-Pesa adapter and sandbox boundary | `@chesangJ` | Accepted (G2) |
| [0005](0005-dual-cicd-lanes.md) | Dual CI/CD lanes (GHA + CodePipeline) | `@emebetgirmay` | Accepted (G1) |
| [0006](0006-idempotency-replay.md) | Idempotency and callback replay | `@chesangJ` | Accepted (G2) |
| [0007](0007-multi-tenancy.md) | Multi-tenancy isolation | `@Moraaalice` | Proposed (Product fills before G2) |
| [0008](0008-b2c-payouts.md) | B2C commission payouts | `@chesangJ` | Accepted (G2) |
| [0009](0009-payments-observability.md) | Payments observability for G3 | `@chesangJ` | Proposed (G3 draft) |

## Reading order for the money path

0004 (who may talk to M-Pesa) then 0006 (charges, timeouts, callback replay) then 0008 (payouts),
then 0009 (how the money path is observed at G3). The rules in 0006 are reused by 0008. Each of the three has an "Implementation" section saying what
is built, where, and how it differs from the wording.

## Known follow-ups

- ADR 0002 and 0003 are still `Proposed`; their owner decides when to accept them.
- ADR 0007 is filled in Product's own PR; update its row here when it lands.
- Items marked "verify against Daraja docs" in 0006 and 0008 are confirmed by a sandbox contract
  test run by the deployed adapter, which does not exist yet.
