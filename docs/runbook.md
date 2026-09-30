# Runbook — TillFlow

**Owner:** Reliability DRI — Emebet Girmay (`@emebetgirmay`)

## First safe actions

| Symptom | First safe action | Recovery signal |
|---|---|---|
| Payments callback lag | Check the Payments log for callback errors; callbacks are synchronous until the queue lands (then: SQS age and DLQ, never redrive blindly) | Callback lag < SLO; Grafana green |
| Uncertain STK (timeout) | Leave pending; run query/reconcile | Terminal paid/failed; no duplicate charge |
| Commission late | Inspect ledger + Payments B2C status; no manual double B2C | Terminal by policy; duplicate = 0 |
| Broken release | Let the pipeline's smoke check roll back; otherwise [roll back by hand](#release-rollback) | Post-deploy smoke pass |
| Database lost or corrupted | [Restore](#restore) to a new instance; never write into the damaged one | Services healthy on the restored instance; reconcile clean |

## Alert contract

Every Slack alert must include: environment, service, symptom, user/SLO impact, observed value, Grafana panel, runbook link, owner, first safe action. Webhook only in Secrets Manager (`devops-g9/slack-webhook`).

Path (ADR 0010): CloudWatch alarm, firing and OK, to SNS `devops-g9-alerts`, to Lambda
`devops-g9-slack-notifier`, to Slack. Each alarm's description is JSON with the fields above;
the Lambda formats them and fills gaps from the alarm itself.

## Slack webhook

Terraform creates the secret `devops-g9/slack-webhook` with no value. CI never reads or writes it.
Until a value is set, the notifier logs `slack_webhook_not_set` and drops the message; alarms
still change state.

Set or rotate it by hand, signed in with your own SSO user (`aws sso login --profile g9`), without
leaving the URL in shell history or a file:

```bash
read -rs SLACK_URL   # paste the https://hooks.slack.com/... URL, then Enter
aws secretsmanager put-secret-value --profile g9 --region eu-north-1 \
  --secret-id devops-g9/slack-webhook \
  --secret-string "$(printf '{"url":"%s"}' "$SLACK_URL")"
unset SLACK_URL
```

The notifier only posts to `https://hooks.slack.com/` URLs and reads the secret on every run, so
no deploy is needed after a change. Check: the next alarm's Lambda log shows `slack_posted`
with `status` 200.

## Alarms (G3, ADR 0010)

All alarms post to Slack when they fire and when they recover. Anchors below are the `runbook`
links in each alarm. Never clear an alarm by forcing its state.

### probe-down

The public `/ready` (API Gateway -> VPC link -> ALB -> POS) failed for 2 minutes, or the probe
stopped running.

1. Check `service-down` alarms. If POS is down too, work that first.
2. If POS is healthy behind the ALB, the edge is the problem: API Gateway, the VPC link or the ALB
   listener. Check the latest `Release` run for infra changes.
3. If only the probe is missing data, read `/aws/lambda/devops-g9-probe` logs.

Recovered: `ProbeSuccess` back to 1 for 2 minutes.

### service-down

No healthy POS or Payments target behind the ALB for 2 minutes.

1. ECS -> `devops-g9` -> the service -> stopped tasks: read the stop reason.
2. If a release caused it, roll back to the last good digest (the pipeline's rollback, not a
   hand-made task definition).
3. Payments down: POS keeps sales pending. Do not mark anything paid or failed by hand.

Recovered: `HealthyHostCount` back to at least 1.

<a id="pos-fast-burn"></a><a id="pos-slow-burn"></a>

### pos-fast-burn, pos-slow-burn

POS 5xx share is spending the 99.9% budget 14.4x (fast, pages) or 6x (slow) too quickly.

1. Check POS `/ready` and whether Payments is healthy (a Payments outage shows up here).
2. Look at the POS log group for the failing route.
3. Do not replay sales by hand; clients retry with the same idempotency key.

<a id="payments-fast-burn"></a><a id="payments-slow-burn"></a>

### payments-fast-burn, payments-slow-burn

Payments 5xx share is spending the 99.5% budget too quickly.

1. Find the failing route in the Payments log group.
2. Never resend a payment or payout to clear it; timeouts stay `UNKNOWN` and reconcile.
3. If payouts are affected, the kill switch is the safe stop (ADR 0008).

### ecs-cpu-high

POS or Payments CPU above 70% for 10 minutes.

1. Compare with request rate: real load or a hot loop?
2. Scale out first (desired count), then investigate; roll back only if a release caused it.

<a id="alb-5xx-fast-burn"></a><a id="alb-5xx-slow-burn"></a>

### alb-5xx-fast-burn, alb-5xx-slow-burn

The load balancer itself is answering 5xx (usually 502) because a target gave no usable
response: it dropped the connection, or crashed mid-request. These never appear in the
services' own 5xx counts, so the per-service burn alarms stay green. Found by the G3 k6 soak:
110 x 502 from an unhandled `RuntimeError` in Payments (fixed; see `k6-analysis.md`).

1. Find the minutes in the Grafana overview panel "ALB-generated 5xx".
2. Search both services' log groups for `Traceback` in those minutes; the traceback names the
   code path. `unhandled_error` lines (Payments) carry the trace id.
3. Check `service-down` and ECS stopped tasks: a task being replaced also produces 502s.
4. Do not restart services blind; an idempotent client retry is already safe.

### Payments app alarms (ADR 0009 section 4)

These read Payments' own metrics (`GET /metrics`, scraped by ADOT) through Metrics Insights
queries, so they see the service's view rather than the ALB's. Owner `@chesangJ` unless noted.

#### payments-critical-anomaly

A contradiction or constraint violation on the money path. Treat as a P0.

1. Trip the payouts kill switch before anything else.
2. Find the anomaly line in the Payments log group (`event` names the kind); inspect that row.
3. Never re-send a payment or payout to "fix" it; resolve with provider evidence.

#### payments-payouts-paused

The kill switch reads 0. Payouts stop until someone re-enables them.

1. Read the trip reason (the flag's note, and the Payments log around the trip time).
2. Fix the cause (funding, credentials, a stuck result) before re-enabling by hand.

#### payments-needs-review

A payment or payout has sat in `NEEDS_REVIEW` for 15 minutes.

1. Check the provider evidence (M-PESA Organization Portal) for that record.
2. Resolve it by hand as the evidence says. Never auto-fail it.

<a id="payments-payment-unknown-too-long"></a><a id="payments-payout-unknown-too-long"></a>

#### payments-payment-unknown-too-long, payments-payout-unknown-too-long

The oldest `UNKNOWN` payment or payout is older than 10 minutes.

1. Run the reconcile pass (`POST /_admin/sweep` from inside the VPC).
2. Leave it pending if the provider still has no answer. A timeout is not a decline, and a
   payout is never resubmitted (ADR 0006, ADR 0008).

#### payments-reconcile-stale

No successful reconcile pass in 15 minutes. Owner `@emebetgirmay`.

1. Check what calls `POST /_admin/sweep` on a schedule, and its logs.
2. Run one pass by hand from inside the VPC; `payments_reconcile_runs_total{result="error"}`
   rising means the pass itself is failing, so read the Payments log.

## Database (RDS PostgreSQL, ADR 0002)

`devops-g9-db`: PostgreSQL 16.15, `db.t4g.micro`, single-AZ, database `tillflow`, one schema and
login role per service (`pos`, `payments`, `commission`); a role can only use its own schema.
Backups daily at 20:00–20:30 UTC (23:00 EAT), kept 7 days, point-in-time restore. Owner `@emebetgirmay`.

<a id="db-bootstrap"></a>

### Bootstrap roles and schemas

After the instance is first created, and after every destroy and rebuild:

```bash
aws sso login --profile g9
./infra/scripts/rds-bootstrap.sh
```

It writes each service's login to `devops-g9/db/<service>` (keys `url`, `sqlalchemy_url`,
`username`, `password`, `host`, `port`, `dbname`, `schema`) and runs the one-off
`devops-g9-db-bootstrap` task inside the VPC. Safe to re-run; passwords are kept unless `ROTATE=1`.
Never paste a password on the command line or in chat: read it with
`aws secretsmanager get-secret-value` only if you must, and never into shell history.

A service moves onto RDS by a reviewed PR setting `pos_database` / `payments_database = "rds"`
once its image has a Postgres driver; its task then gets `DATABASE_URL` from its own secret.
Rollback is the same PR reverted: the service goes back to its SQLite file (empty after the move).

### db-cpu-high

RDS CPU above 80% for 10 minutes.

1. Performance Insights (RDS console, `devops-g9-db`): which statement, and which role (service)?
2. Did a release or a Commission run (00:00–06:30 EAT) start it? Commission's disburse passes are
   expected load; do not resize the instance during them.
3. A missing index or runaway query is a fix in that service's code; a resize is a PR on
   `instance_class`, applied outside Commission's window.

### db-storage-low

Free storage below 2 GiB. Storage autoscales up to 50 GiB (`max_allocated_storage`).

1. Which schema grows: `SELECT schemaname, pg_size_pretty(sum(pg_total_relation_size(schemaname||'.'||tablename))) FROM pg_tables GROUP BY 1;`
2. Raise `max_allocated_storage` by PR before it fills. Never delete rows to make room: payments,
   payouts and the ledger are money records.

### db-memory-low

Freeable memory below 100 MiB for 10 minutes (the instance has 1 GiB).

1. Connections per role: `SELECT usename, count(*) FROM pg_stat_activity GROUP BY 1;`
2. A service holding many connections needs a smaller pool; if it persists under normal load,
   move to `db.t4g.small` by PR.

<a id="release-rollback"></a>

## Release rollback

The release pipeline rolls a service back by itself when its post-deploy smoke check fails
(`release.yml`, "Rollback previous task definition on smoke failure"); this happened for real on
2026-09-29 ([scar log](scar-log.md)). By hand, when a release passed smoke but is wrong:

1. Find the last good revision: `aws ecs list-task-definitions --family-prefix devops-g9-<service> --sort DESC --max-items 5`.
2. `aws ecs update-service --cluster devops-g9 --service devops-g9-<service> --task-definition devops-g9-<service>:<revision>`
   then `aws ecs wait services-stable --cluster devops-g9 --services devops-g9-<service>`.
3. Revert the PR on `main` so the next release does not bring it back.
4. Schema changes: a service's migration must be backward compatible for one release, so the
   previous image still runs on the new schema; if it is not, restore instead.

<a id="restore"></a>

## Restore (RDS point-in-time, G4)

**When:** data lost or corrupted (a bad migration, a destructive statement), or the instance is
gone. **Never** write into the damaged instance to "fix" it: restore beside it, check, then switch.
RPO: point-in-time restore reaches within about 5 minutes of the incident (7 days kept). The drill
measures RTO.

1. **Declare and freeze.** Post in Slack: restore started, time, reason. Disable the Commission
   schedules (no payout runs against data that may be rolled back). Note the restore point `T`: a
   moment just before the damage (or `--use-latest-restorable-time` if the instance is gone).
   (Commission's schedules exist once it is deployed; until then there is nothing to disable.)
2. **Restore beside it** (inherits encryption; the service roles and their passwords come with the data):
   ```bash
   aws rds restore-db-instance-to-point-in-time \
     --source-db-instance-identifier devops-g9-db --target-db-instance-identifier devops-g9-db-restore \
     --restore-time "$T" --db-subnet-group-name devops-g9-db --db-parameter-group-name devops-g9-pg16 \
     --vpc-security-group-ids "$(aws ec2 describe-security-groups --filters Name=group-name,Values=devops-g9-db --query 'SecurityGroups[0].GroupId' --output text)" \
     --no-publicly-accessible --no-multi-az --manage-master-user-password \
     --tags Key=group,Value=devops-g9 Key=owner,Value=emebetgirmay Key=environment,Value=sandbox \
            Key=service,Value=platform Key=managed-by,Value=terraform Key=capstone,Value=tillflow
   aws rds wait db-instance-available --db-instance-identifier devops-g9-db-restore
   ```
3. **Check it** before any traffic: row counts per schema and the newest row times, against the
   incident timeline; Payments' invariants (`GET /_admin/invariants` once a service points at it).
4. **Switch.** Rename the damaged instance to `devops-g9-db-damaged` (wait until it is
   available), then the restored one to `devops-g9-db`, so the endpoint in every service secret is
   valid again; force a new deployment of each service on RDS. Terraform tracks the instance by
   its resource ID, so move state to the restored one before the next release:
   `terraform state rm aws_db_instance.main && terraform import aws_db_instance.main devops-g9-db`,
   then `terraform plan` must show no replacement of `aws_db_instance.main`.
5. **Reconcile money.** Payments' sweep (`POST /_admin/sweep`) re-queries Daraja for every payment
   and payout not terminal in the restored data: anything that completed after `T` is recovered
   from the provider, not re-sent. POS then catches up its sales from Payments. Only after that,
   re-enable the Commission schedules.
6. **Declare recovery** in Slack with RTO (declare → services healthy on the restored instance) and
   RPO (`T` → last change lost), and add a line to the [scar log](scar-log.md). Delete
   `devops-g9-db-damaged` only after the review, with a final snapshot.

