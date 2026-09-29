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

## Restore order (G4)

1. Restore DB/S3 to safe target  
2. Verify RPO/RTO  
3. Reconcile Daraja provider references  
4. Declare recovery
