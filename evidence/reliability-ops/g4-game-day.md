# G4 game day: plan and results

**Owner:** Emebet Girmay (`@emebetgirmay`), Reliability + operations · **Runs:** once POS and
Payments are on RDS (ADR 0002) · **Adapter:** FakeAdapter only · **Runbook:** [`docs/runbook.md`](../../docs/runbook.md)

Every scenario breaks a real component: never `SetAlarmState`. Each one needs a Slack post from the
alarm when it fires **and** when it recovers, the runbook section followed as written (a step that
was wrong is fixed in the runbook the same day), and the money invariants holding afterwards
(`GET /_admin/invariants`).

## Roles

| Role | Who |
|---|---|
| Incident commander, scribe (timeline, UTC) | Emebet (`@emebetgirmay`) |
| Payments and Commission checks: invariants, reconcile, no duplicate payout | Joy (`@chesangJ`) |
| POS checks: sales before and after, attendant view | Alice (`@Moraaalice`) |

## Scenarios

| # | Break | Expected signal | Pass when |
|---|---|---|---|
| 1 | **Restore:** point-in-time restore of `devops-g9-db` to `devops-g9-db-restore` at a time `T` after a known set of sales and payments; switch the services to it ([runbook "Restore"](../../docs/runbook.md#restore)) | Planned: announced in Slack | RPO and RTO measured and written below; every sale and payment up to `T` present; Payments' sweep resolves anything in flight from the provider, with no second charge or payout; invariants hold |
| 2 | **Payments task killed:** `aws ecs stop-task` on the only Payments task | `payments-down` fires and recovers; `probe-down` stays OK (POS still ready) | ECS replaces the task by itself; **no data lost** across the restart (counts before = after, which SQLite could not do); invariants hold |
| 3 | **Database reboot:** `aws rds reboot-db-instance --db-instance-identifier devops-g9-db` during light k6 traffic | Burn-rate alarms may fire briefly; RDS event in the log | Both services recover without a redeploy (connection pools reconnect); failed requests are 5xx, never a wrong state; invariants hold |
| 4 | **Poisoned callback:** a callback Payments cannot apply, repeated until it lands in the DLQ | DLQ alarm fires | The callback is in the DLQ, not lost or half-applied; the runbook's redrive steps work. *(Needs the SQS queue; skipped until it lands, and said so below.)* |

## Results

*To fill on the day.*

| # | Start (UTC) | Fired | Recovered | RTO | RPO | Invariants | Runbook fixes | Evidence |
|---|---|---|---|---|---|---|---|---|
| 1 | | | | | | | | |
| 2 | | | | | | | | |
| 3 | | | | | | | | |
| 4 | | | | | | | | |

## Timeline

*Scribe's notes, UTC, one line per event.*
