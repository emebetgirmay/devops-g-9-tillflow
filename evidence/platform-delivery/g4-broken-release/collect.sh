#!/usr/bin/env bash
# G4 broken-release evidence: a release whose new POS image crashed at start-up, caught by the
# pipeline's smoke check and rolled back automatically (Release run #46, 2026-09-29).
#
# Writes, next to this script:
#   pipeline-steps.json   every step of the "Build scan push deploy POS" job with UTC times
#   pipeline-log.txt      the job log lines that show the failure and the rollback
#   user-impact.json      per minute: edge probe success, POS healthy targets; totals of 5xx
#   alarm-changes.json    alarm state changes in the window (none expected: nothing user-facing broke)
#   crash-log.txt         the new revision's start-up failure from /devops-g9/pos
#   checks.json           pass/fail for each claim in README.md; exits 1 if any fails
#
# Read-only. Needs gh (repo read) and an SSO session: aws sso login --profile g9

set -euo pipefail

export AWS_PROFILE="${AWS_PROFILE:-g9}" AWS_REGION="${AWS_REGION:-eu-north-1}"
REPO="${REPO:-emebetgirmay/devops-g-9-tillflow}"
RUN_ID="${RUN_ID:-36625900401}"   # Release #46, "Merge pull request #52"
FROM="${FROM:-2026-09-29T20:22:00Z}"
TO="${TO:-2026-09-29T20:42:00Z}"
OUT="$(cd "$(dirname "$0")" && pwd)"

job=$(gh api "repos/$REPO/actions/runs/$RUN_ID/jobs" \
  -q '.jobs[] | select(.name == "Build scan push deploy POS") | .id')
gh api "repos/$REPO/actions/jobs/$job" \
  -q '[.steps[] | {step: .name, started: .started_at, completed: .completed_at, conclusion}]' \
  >"$OUT/pipeline-steps.json"
gh api "repos/$REPO/actions/jobs/$job/logs" | sed 's/^[0-9T:.Z-]* //' \
  | grep -E 'Waiter ServicesStable failed|Smoke failed|rolling back|"failedTasks": [1-9]|task-definition/devops-g9-pos:[0-9]+" \\' \
  | sed 's/\x1b\[[0-9;]*m//g' >"$OUT/pipeline-log.txt"

lb=$(aws elbv2 describe-load-balancers --names devops-g9-alb \
  --query 'LoadBalancers[0].LoadBalancerArn' --output text | sed 's#.*:loadbalancer/##')
tg=$(aws elbv2 describe-target-groups --names devops-g9-pos \
  --query 'TargetGroups[0].TargetGroupArn' --output text | sed 's#.*:##')

series() {  # namespace metric stat dims...
  local ns=$1 metric=$2 stat=$3; shift 3
  aws cloudwatch get-metric-statistics --namespace "$ns" --metric-name "$metric" --dimensions "$@" \
    --start-time "$FROM" --end-time "$TO" --period 60 --statistics "$stat" \
    --query "sort_by(Datapoints,&Timestamp)[].{t:Timestamp,v:$stat}" --output json
}
total() {  # namespace metric dims...
  local ns=$1 metric=$2; shift 2
  aws cloudwatch get-metric-statistics --namespace "$ns" --metric-name "$metric" --dimensions "$@" \
    --start-time "$FROM" --end-time "$TO" --period 1200 --statistics Sum \
    --query 'Datapoints[0].Sum' --output text
}

probe=$(series TillFlow/Probe ProbeSuccess Minimum Name=Target,Value=edge-ready)
healthy=$(series AWS/ApplicationELB HealthyHostCount Minimum \
  Name=LoadBalancer,Value="$lb" Name=TargetGroup,Value="$tg")
pos_5xx=$(total AWS/ApplicationELB HTTPCode_Target_5XX_Count Name=LoadBalancer,Value="$lb" Name=TargetGroup,Value="$tg")
alb_5xx=$(total AWS/ApplicationELB HTTPCode_ELB_5XX_Count Name=LoadBalancer,Value="$lb")
pos_requests=$(total AWS/ApplicationELB RequestCount Name=LoadBalancer,Value="$lb" Name=TargetGroup,Value="$tg")

python3 - "$OUT" "$FROM" "$TO" "$pos_5xx" "$alb_5xx" "$pos_requests" "$probe" "$healthy" <<'PY'
import json, sys
out, frm, to, pos5, alb5, reqs, probe, healthy = sys.argv[1:]
num = lambda v: 0.0 if v in ("None", "") else float(v)
json.dump({"window_utc": [frm, to], "edge_probe_min_per_minute": json.loads(probe),
           "pos_healthy_targets_min_per_minute": json.loads(healthy),
           "pos_target_5xx": num(pos5), "alb_generated_5xx": num(alb5),
           "pos_requests": num(reqs)}, open(f"{out}/user-impact.json", "w"), indent=2)
PY

aws cloudwatch describe-alarm-history --history-item-type StateUpdate --start-date "$FROM" --end-date "$TO" \
  --query 'AlarmHistoryItems[?starts_with(AlarmName, `devops-g9-`)].{time:Timestamp,alarm:AlarmName,change:HistorySummary}' \
  --output json >"$OUT/alarm-changes.json"

from_ms=$(( $(date -u -d "$FROM" +%s) * 1000 )); to_ms=$(( $(date -u -d "$TO" +%s) * 1000 ))
aws logs filter-log-events --log-group-name /devops-g9/pos --start-time "$from_ms" --end-time "$to_ms" \
  --filter-pattern '?"startup failed" ?"requires fastapi" ?"Started server process"' \
  --query 'events[].[timestamp,logStreamName,message]' --output text \
  | while IFS=$'\t' read -r ts stream msg; do
      printf '%s  %s  %s\n' "$(date -u -d "@$((ts / 1000))" +%FT%TZ)" "${stream##*/}" "$msg"
    done >"$OUT/crash-log.txt"

python3 - "$OUT" <<'PY'
import json, sys
from pathlib import Path
out = Path(sys.argv[1])
steps = {s["step"]: s for s in json.loads((out / "pipeline-steps.json").read_text())}
impact = json.loads((out / "user-impact.json").read_text())
log = (out / "pipeline-log.txt").read_text()
crash = (out / "crash-log.txt").read_text()
probe = [p["v"] for p in impact["edge_probe_min_per_minute"]]
healthy = [h["v"] for h in impact["pos_healthy_targets_min_per_minute"]]
alarms = json.loads((out / "alarm-changes.json").read_text())
checks = {
    "smoke_check_failed": steps["Wait stable + mandatory smoke via API Gateway"]["conclusion"] == "failure",
    "automatic_rollback_succeeded": steps["Rollback previous task definition on smoke failure"]["conclusion"] == "success",
    "pipeline_logged_rollback_target": "Smoke failed" in log and "rolling back" in log,
    "new_revision_crashed_at_startup": "Application startup failed" in crash,
    "edge_probe_never_failed": bool(probe) and min(probe) >= 1,
    "pos_always_had_a_healthy_target": bool(healthy) and min(healthy) >= 1,
    "no_pos_5xx": impact["pos_target_5xx"] == 0,
    "no_alb_generated_5xx": impact["alb_generated_5xx"] == 0,
    "no_user_facing_alarm_fired": not any("to ALARM" in a["change"] for a in alarms),
}
(out / "checks.json").write_text(json.dumps(checks, indent=2) + "\n")
for name, ok in checks.items():
    print(("PASS " if ok else "FAIL ") + name)
sys.exit(0 if all(checks.values()) else 1)
PY
