# Runbook — TillFlow (stub for G0; complete by G4)

**Owner:** Reliability DRI — Emebet Girmay (`@emebetgirmay`)

## First safe actions (draft)

| Symptom | First safe action | Recovery signal |
|---|---|---|
| Payments callback lag | Check SQS age / DLQ; do not redrive blindly | Callback lag < SLO; Grafana green |
| Uncertain STK (timeout) | Leave pending; run query/reconcile | Terminal paid/failed; no duplicate charge |
| Commission late | Inspect ledger + Payments B2C status; no manual double B2C | Terminal by policy; duplicate = 0 |
| Broken release | Stop promotions; ECS rollback to last good digest | Post-deploy smoke pass |
| Cache down | Expect higher RDS load; scale/repair Redis | p95 recovered; error rate down |

## Alert contract

Every Slack alert must include: environment, service, symptom, user/SLO impact, observed value, Grafana panel, runbook link, owner, first safe action. Webhook only in Secrets Manager (`devops-g9/slack-webhook`).

## Restore order (G4)

1. Restore DB/S3 to safe target  
2. Verify RPO/RTO  
3. Reconcile Daraja provider references  
4. Declare recovery
