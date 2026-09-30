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
| 2 | 16:12:43 | None: `payments-down` needs 2 min (by design); the ALB 5xx burn alarm **should have and did not** (finding 1) | 16:13:59 (new task serving) | 76 s from stop; 40 s of failed reads | 0: nothing lost | Held: `/_admin/invariants` all true ([`invariants-after.json`](g4-gameday/invariants-after.json)); payments unchanged, idempotency key replays | Finding 1 fixed (alarm maths); finding 2 open | [`g4-gameday/`](g4-gameday/) 5/5 checks |
| 3 | | | | | | | | |
| 4 | | | | | | | | |

## Timeline

*Scribe's notes, UTC, one line per event.*

**2026-09-30, scenario 2 (Payments task killed)**, run by `@emebetgirmay` with
[`g4-gameday/payments-task-killed.sh`](g4-gameday/payments-task-killed.sh), Payments on RDS since #78:

- 16:07 Payments rev 34 live on RDS: `DATABASE_URL` from `devops-g9/db/payments` only; RDS connections 0 → 2.
- 16:12:33 One settled payment (`SUCCEEDED`) and one pending (`PENDING`) written through the public URL.
- 16:12:43 `aws ecs stop-task` on the only Payments task.
- 16:13:20 First failed read: the ALB answers 503 (no healthy target); 7 in all.
- 16:13:59 First good read, served by the replacement task.
- 16:14:04 Service stable. Both payments read back unchanged; the same Idempotency-Key returns the
  original payment (no second charge). [`checks.json`](g4-gameday/checks.json) 5/5,
  [`timeline.json`](g4-gameday/timeline.json).

## Findings

1. **The ALB 5xx burn alarm could not see a total outage.** It divided ALB-generated 5xx by
   `RequestCount`, which only counts requests the ALB could send to a target; the 503s for "no
   healthy target" are not in it (16:13: 7 errors, 5 requests). With every request failing the
   ratio read 0. **Fixed:** the denominator is now requests plus ALB errors (`alarms.tf`); on this
   window it reads 20.6% against the 1.44% fast-burn line.
2. **Short outages can fall between 5-minute buckets.** The burn alarms evaluate fixed 5-minute
   periods; a 40-second burst whose datapoints arrive late may never be seen in its bucket. Probable,
   not proven from this run. Proposed: evaluate the fast burn over 1-minute periods, 2 of the last 5
   breaching; decide after the re-run of this scenario with finding 1's fix live.
3. **Paging threshold, by design:** a self-healing 40-second loss of one task does not page
   (`payments-down` needs 2 minutes). It spends about 0.3% of Payments' 28-day budget.
