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
| 1 | 17:20:51 (declared) | Planned, announced | 17:39:34 (restored instance verified) | **18 min 44 s** to a verified restore (RDS restore 17 min 45 s); switching services adds a redeploy | **2 min 36 s** (restore point 17:18:15) | Before-markers present, after-markers absent, as the `payments` and `pos` roles | Runbook step 2 fixed: `--manage-master-user-password` rejected | [`g4-restore/`](g4-restore/) 6/6 checks |
| 2 | 16:12:43 | None: `payments-down` needs 2 min (by design); the ALB 5xx burn alarm **should have and did not** (finding 1) | 16:13:59 (new task serving) | 76 s from stop; 40 s of failed reads | 0: nothing lost | Held: `/_admin/invariants` all true ([`invariants-after.json`](g4-gameday/invariants-after.json)); payments unchanged, idempotency key replays | Finding 1 fixed (alarm maths); finding 2 open | [`g4-gameday/`](g4-gameday/) 5/5 checks |
| 3 | 17:44:41 | `payments-fast-burn` 17:47:07 → OK 17:51:07; `pos-fast-burn` 17:47:53 → OK 17:53:53 (Slack) | POS 17:45:33, Payments 17:45:59 | Database away 43 s (RDS events); POS failed 31 s, Payments 58 s; no task replaced, no redeploy | 0 | Held ([`db-reboot-checks.json`](g4-gameday/db-reboot-checks.json)) | Finding 6 (Payments pool) for `@chesangJ` | [`db-reboot-*`](g4-gameday/) 4/4 checks |
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

**2026-09-30, scenario 1 (restore)**, run by `@emebetgirmay` with
[`g4-restore/restore-drill.sh`](g4-restore/restore-drill.sh), POS and Payments both on RDS:

- 17:15:24 "Before" markers written: a payment and a POS tenant.
- 17:18:15 RDS latest restorable time passes them: the restore point `T`.
- 17:20:51 "After" markers written (the writes a disaster now would lose), then **declared**.
- 17:20:55 Point-in-time restore of `devops-g9-db` to `T` into `devops-g9-db-restore` started.
- 17:38:40 Restored instance available.
- 17:39:34 Verified inside the VPC as the services' own roles: 7 payments and 5 tenants; the
  before-markers present, the after-markers absent ([`verify-log.txt`](g4-restore/verify-log.txt),
  [`checks.json`](g4-restore/checks.json) 6/6, [`timeline.json`](g4-restore/timeline.json)).
- 17:39:36 Restored instance deleted. The live instance and services were not touched.

An earlier attempt at 16:40 stopped at the restore call (finding 4, fixed in the runbook), and a
second one was cut off by a lost operator session after the instance was created; that instance was
deleted and the drill run again from the start.

**2026-09-30, scenario 3 (database reboot)**, run by `@emebetgirmay` with
[`g4-gameday/db-reboot.sh`](g4-gameday/db-reboot.sh), probing POS `/ready` and a Payments read through
the public URL (295 probes, [`db-reboot-probes.json`](g4-gameday/db-reboot-probes.json)):

- 17:44:41 `aws rds reboot-db-instance devops-g9-db`.
- 17:44:50 RDS: shutdown. POS `/ready` answers 503 at once; Payments reads hang to the probe's 10 s timeout.
- 17:45:33 RDS: restarted. POS ready again the same second.
- 17:45:59 Payments answers again, 26 s after the database.
- 17:47:07 / 17:47:53 Fast-burn alarms page for Payments and POS; OK again by 17:51 / 17:53.
- No task was replaced and nothing was redeployed: both services reconnected by themselves;
  the payment written before the reboot is intact and the invariants hold.

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
4. **The runbook's restore command could not run.** Step 2 passed `--manage-master-user-password`,
   which point-in-time restore of PostgreSQL rejects (`InvalidParameterValue`). Not needed: every
   login, master and service roles, comes back with the data. **Fixed** in the runbook; found only
   because the drill ran the documented command as written.
5. **RTO is dominated by RDS itself** (17 min 45 s of 18 min 44 s). A faster restore would need a
   warm standby (Multi-AZ, or a read replica to promote), a cost decision recorded in ADR 0002,
   not a runbook change.
6. **Payments hangs instead of failing fast while the database is away.** Its connection pool
   waits up to 30 s for a connection (`timeout=30` in `services/payments/core/db.py`), sets no
   connection timeout and does not check a connection before handing it out, so during the reboot
   requests queued to the client's timeout and Payments came back 26 s after the database, where
   POS (which answers 503 at once) came back the same second. **Proposed, `@chesangJ`:** pool
   `timeout` of a few seconds, `connect_timeout` in the connection arguments, and
   `check=ConnectionPool.check_connection`, then re-run this scenario.
7. **The burn alarms work for an outage longer than a minute:** both fast-burn alarms paged and
   recovered in Slack for a 31–58 s outage at low traffic, which is the behaviour finding 2
   questioned for a 40 s one; that scenario's re-run is still due.

