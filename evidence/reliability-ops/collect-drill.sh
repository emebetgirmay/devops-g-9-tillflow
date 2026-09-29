#!/usr/bin/env bash
# G3 Slack drill evidence (ADR 0010 section 4): a real symptom, never SetAlarmState.
#
# The drill stops the scheduled reconcile pass (reconcile_sweep_enabled = false, via a PR), waits
# for devops-g9-payments-reconcile-stale to fire, then re-enables it and waits for recovery.
# This script records, for the drill window, that each step really happened and reached Slack:
#
#   drill-schedule.json        the sweep schedule's current state
#   drill-sweep-runs.json      the sweep Lambda's own log lines (runs stop, then resume)
#   drill-alarm-history.json   the alarm's state changes (OK -> ALARM -> OK)
#   drill-sns-published.json   messages published to devops-g9-alerts, per 5 minutes
#   drill-slack-posts.json     the notifier's slack_posted lines for this alarm (status 200)
#   drill-checks.json          pass/fail for each of the above; the script exits 1 if any fails
#
# Read-only. Needs an SSO session: aws sso login --profile g9
#
#   DRILL_FROM=2026-09-29T16:45:00Z DRILL_TO=2026-09-29T17:40:00Z \
#     ./evidence/reliability-ops/collect-drill.sh

set -euo pipefail

export AWS_PROFILE="${AWS_PROFILE:-g9}" AWS_REGION="${AWS_REGION:-eu-north-1}"
: "${DRILL_FROM:?set DRILL_FROM (UTC, e.g. 2026-09-29T16:45:00Z)}"
DRILL_TO="${DRILL_TO:-$(date -u +%FT%TZ)}"

ALARM=devops-g9-payments-reconcile-stale
OUT="$(cd "$(dirname "$0")" && pwd)"
from_ms=$(( $(date -u -d "$DRILL_FROM" +%s) * 1000 ))
to_ms=$(( $(date -u -d "$DRILL_TO" +%s) * 1000 ))

echo "drill window: $DRILL_FROM -> $DRILL_TO"

aws scheduler get-schedule --name devops-g9-reconcile-sweep \
  --query '{name:Name,state:State,expression:ScheduleExpression,target:Target.Arn}' \
  --output json >"$OUT/drill-schedule.json"

aws logs filter-log-events --log-group-name /aws/lambda/devops-g9-reconcile-sweep \
  --start-time "$from_ms" --end-time "$to_ms" --filter-pattern '"reconcile_sweep_"' \
  --query 'events[].{time:timestamp,message:message}' --output json >"$OUT/drill-sweep-runs.json"

aws cloudwatch describe-alarm-history --alarm-name "$ALARM" --history-item-type StateUpdate \
  --start-date "$DRILL_FROM" --end-date "$DRILL_TO" \
  --query 'AlarmHistoryItems[].{time:Timestamp,summary:HistorySummary}' \
  --output json >"$OUT/drill-alarm-history.json"

aws cloudwatch get-metric-statistics --namespace AWS/SNS --metric-name NumberOfMessagesPublished \
  --dimensions Name=TopicName,Value=devops-g9-alerts \
  --start-time "$DRILL_FROM" --end-time "$DRILL_TO" --period 300 --statistics Sum \
  --query 'sort_by(Datapoints,&Timestamp)[].{time:Timestamp,published:Sum}' \
  --output json >"$OUT/drill-sns-published.json"

aws logs filter-log-events --log-group-name /aws/lambda/devops-g9-slack-notifier \
  --start-time "$from_ms" --end-time "$to_ms" --filter-pattern "\"$ALARM\"" \
  --query 'events[].{time:timestamp,message:message}' --output json >"$OUT/drill-slack-posts.json"

python3 - "$OUT" "$ALARM" <<'PY'
import json, sys
from pathlib import Path

out, alarm = Path(sys.argv[1]), sys.argv[2]
load = lambda name: json.loads((out / name).read_text() or "[]")

history = [h["summary"] for h in load("drill-alarm-history.json")]
posts = [json.loads(e["message"]) for e in load("drill-slack-posts.json")
         if e["message"].lstrip().startswith("{")]
sweeps = [e["message"] for e in load("drill-sweep-runs.json")]
published = sum(p["published"] for p in load("drill-sns-published.json"))

checks = {
    "alarm_went_ok_to_alarm": any("to ALARM" in h for h in history),
    "alarm_recovered_alarm_to_ok": any("from ALARM to OK" in h for h in history),
    "slack_posted_firing_200": any(p.get("state") == "ALARM" and p.get("status") == 200 for p in posts),
    "slack_posted_recovered_200": any(p.get("state") == "OK" and p.get("status") == 200 for p in posts),
    "sns_published_at_least_2": published >= 2,
    "sweep_resumed_after_drill": any("reconcile_sweep_ok" in m for m in sweeps),
}
result = {"alarm": alarm, "checks": checks, "passed": all(checks.values()),
          "history": history, "slack_posts": posts, "sns_published": published}
(out / "drill-checks.json").write_text(json.dumps(result, indent=2) + "\n")
for name, ok in checks.items():
    print(("PASS " if ok else "FAIL ") + name)
sys.exit(0 if result["passed"] else 1)
PY
