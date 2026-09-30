# G4 Recover and G5 Release: evidence and sign-off

**Group 9, TillFlow** · Region `eu-north-1` · Public URL (since the G5 rebuild):
https://nilrqzkq8a.execute-api.eu-north-1.amazonaws.com · Updated 2026-09-30

Every drill below broke or rebuilt a real component; none used `SetAlarmState`. Load and drills
use the FakeAdapter only.

## G4 Recover: failure drills and restore

| Requirement | Result | Evidence | Owner |
|---|---|---|---|
| **Restore with measured RPO and RTO** | Point-in-time restore beside the live instance: **RPO 2 min 36 s, RTO 18 min 44 s**, 6/6 checks (before-markers present, after-markers absent, as the services' own roles). Found and fixed a runbook restore command that could not run | [`reliability-ops/g4-game-day.md`](reliability-ops/g4-game-day.md) scenario 1, [`g4-restore/`](reliability-ops/g4-restore/) | `@emebetgirmay` |
| **Broken release** | A POS build crashed at start-up; the release's smoke check rolled it back to the previous revision automatically, no user impact, 9/9 checks | [`platform-delivery/g4-broken-release/`](platform-delivery/g4-broken-release/README.md) | `@emebetgirmay` |
| **Service task killed** | Payments' only task stopped on RDS: served again after 76 s, nothing lost, idempotency key replays, invariants hold, 5/5. Found and fixed an alarm that could not see a total outage; the re-run (37 s) proved the fix: it paged in Slack within 3 minutes | g4-game-day.md scenario 2, [`g4-gameday/`](reliability-ops/g4-gameday/) | `@emebetgirmay` |
| **Database failure** | RDS rebooted under traffic (43 s away): both services recovered without intervention, fast-burn alarms paged and recovered in Slack, 4/4. Found Payments waiting 30 s instead of failing fast (finding 6): fixed in #88 and proven by a re-run on RDS (one fast error, no hanging request) | g4-game-day.md scenario 3 | `@emebetgirmay`, fix `@chesangJ` |
| **Uncertain payment and payout, callback replay** | A timeout is never a decline; replayed and reordered callbacks change nothing; also on PostgreSQL | [`payments-integrity/g4/`](payments-integrity/g4/), [`postgres/g4-checks.json`](payments-integrity/postgres/g4-checks.json) | `@chesangJ` |
| **A sale stuck on Payments' no-push gap** | POS refuses an unsafe retry and recovers the sale by reconcile, with no second STK push | [`product-pos/README.md`](product-pos/README.md) "G4", `g4-recover-*` | `@Moraaalice` |
| **Runbook complete and rehearsed** | Alarms, Slack webhook, database, release rollback, restore, destroy and rebuild, Commission; every drill ran the documented steps and fixed what was wrong | [`docs/runbook.md`](../docs/runbook.md) | `@emebetgirmay` |

## G5 Release: fresh release and defences

| Requirement | Result | Evidence | Owner |
|---|---|---|---|
| **Destroy and rebuild from code** | Everything destroyed and rebuilt from `main`: **green 43 min after the destroy started** (rebuild 20 min), 6/6 smoke checks through the new URL, nothing billable left behind | [`platform-delivery/g5-destroy-rebuild/`](platform-delivery/g5-destroy-rebuild/README.md) | `@emebetgirmay` |
| **Fresh release** | The rebuilt stack's first release built, scanned and deployed POS, Payments and Commission, each with its smoke check (run 36768391223) | same | `@emebetgirmay` |
| **Managed database** | POS, Payments and Commission on RDS PostgreSQL 16: one schema and one role per service, credentials only in Secrets Manager, encrypted, private, 7-day point-in-time backups | [ADR 0002](../docs/adrs/0002-rds-postgresql.md) | `@emebetgirmay`, `@chesangJ`, `@Moraaalice` |
| **Supply chain** | Image digests only, immutable tags, gitleaks, Trivy config and image scans failing on HIGH/CRITICAL, a CycloneDX SBOM per released image; accepted risks reviewed 2026-09-30 | `release.yml`, `pr.yml`, [`.trivyignore`](../.trivyignore) | `@emebetgirmay` |
| **Edge defences** | Operator and test paths refused from the internet on real-adapter builds; `/metrics` not public; callback source allowlist | [production readiness](../docs/production-readiness.md) | `@chesangJ`, `@emebetgirmay` |
| **Tracing** | X-Ray waterfall from sale to payment to callback in one trace | [`reliability-ops/g3-evidence.md`](reliability-ops/g3-evidence.md) "Trace" | `@chesangJ`, `@emebetgirmay` |
| **Commission deployed** | Scheduled ECS tasks on RDS; the release smoke-runs one `disburse.py` pass; 06:30 EAT alarm defined | PR #84, runbook "Commission" | `@emebetgirmay`, `@chesangJ` |
| **Cost** | About $151 a month with RDS and Commission, unit prices from the bill | [`docs/cost-model.md`](../docs/cost-model.md) | `@emebetgirmay` |

## Not done yet, stated

| Item | State | Owner |
|---|---|---|
| Commission schedules switched on | Infrastructure and release live; `disburse.py --check` is built (it prints `{"event": "payouts_not_terminal", "count": N}`); waits for a sandbox tenant id (the rebuild emptied the database) | `@chesangJ`, `@Moraaalice`, then `@emebetgirmay` |
| Callbacks on SQS with a DLQ, and G4 scenario 4 (poisoned callback) | **Not built, and not before the viva.** Callbacks are handled synchronously; the scheduled reconcile is the safety net for a lost one (ADR 0009 question 5). Design agreed: one standard queue `devops-g9-payments-callbacks` for STK and B2C results, redrive to `devops-g9-payments-callbacks-dlq` after 5 receives, alarm on any DLQ message | `@chesangJ` (code), `@emebetgirmay` (queue, alarm) |

## Sign-off

Each person signs their own row after reading this page and the evidence for their area.

| Name | Role | G4 signed | G5 signed |
|---|---|---|---|
| Emebet Girmay (`@emebetgirmay`) | Platform + delivery; Reliability + operations | | |
| Mitingi Joy Chesang (`@chesangJ`) | Payments + integrity | | |
| Alice Moraa (`@Moraaalice`) | Product + POS | | |
