#!/usr/bin/env bash
# Run the G3 k6 envelope inside the VPC and keep its results (ADR 0010 section 5, R-8).
#
# Starts one devops-g9-k6 task (infra/envs/sandbox/k6.tf) in the private subnets, waits for it
# to finish (about 17 minutes: smoke, stepped, spike, 15-minute soak), then writes:
#
#   k6-g3-run.txt       the k6 console output (progress bars stripped)
#   k6-g3-summary.json  k6's end-of-test summary (--summary-export)
#   k6-g3-meta.json     task ARN, start/stop times (UTC) and k6's exit code
#
# FakeAdapter only: check first that Payments runs MPESA_ADAPTER=fake. Tell the team before
# starting; the burn alarms may page if the spike pushes 5xx or latency over budget.
#
#   aws sso login --profile g9
#   ./evidence/reliability-ops/run-k6.sh

set -euo pipefail

export AWS_PROFILE="${AWS_PROFILE:-g9}" AWS_REGION="${AWS_REGION:-eu-north-1}"
OUT="$(cd "$(dirname "$0")" && pwd)"
CLUSTER=devops-g9

adapter=$(aws ecs describe-task-definition --task-definition devops-g9-payments \
  --query "taskDefinition.containerDefinitions[?name=='payments'].environment[] | [?name=='MPESA_ADAPTER'].value | [0]" \
  --output text)
service_td=$(aws ecs describe-services --cluster "$CLUSTER" --services devops-g9-payments \
  --query 'services[0].taskDefinition' --output text)
running_adapter=$(aws ecs describe-task-definition --task-definition "$service_td" \
  --query "taskDefinition.containerDefinitions[?name=='payments'].environment[] | [?name=='MPESA_ADAPTER'].value | [0]" \
  --output text)
if [[ "$running_adapter" != "fake" ]]; then
  echo "refusing: running Payments uses MPESA_ADAPTER=$running_adapter (latest registered: $adapter); k6 is fake-only" >&2
  exit 2
fi

subnets=$(aws ec2 describe-subnets --filters "Name=tag:Name,Values=devops-g9-private-*" \
  --query 'Subnets[].SubnetId' --output text | tr '\t' ',')
sg=$(aws ec2 describe-security-groups --filters Name=group-name,Values=devops-g9-k6 \
  --query 'SecurityGroups[0].GroupId' --output text)

started=$(date -u +%FT%TZ)
task=$(aws ecs run-task --cluster "$CLUSTER" --launch-type FARGATE --task-definition devops-g9-k6 \
  --network-configuration "awsvpcConfiguration={subnets=[$subnets],securityGroups=[$sg],assignPublicIp=DISABLED}" \
  --started-by g3-k6 --query 'tasks[0].taskArn' --output text)
echo "started $task at $started (Payments adapter: $running_adapter)"
echo "watch it live in Grafana: TillFlow Payments / TillFlow overview"

while :; do
  status=$(aws ecs describe-tasks --cluster "$CLUSTER" --tasks "$task" \
    --query 'tasks[0].lastStatus' --output text)
  echo "$(date -u +%T) $status"
  [[ "$status" == "STOPPED" ]] && break
  sleep 60
done
stopped=$(date -u +%FT%TZ)

exit_code=$(aws ecs describe-tasks --cluster "$CLUSTER" --tasks "$task" \
  --query "tasks[0].containers[?name=='k6'].exitCode | [0]" --output text)
reason=$(aws ecs describe-tasks --cluster "$CLUSTER" --tasks "$task" \
  --query 'tasks[0].stoppedReason' --output text)

stream="k6/k6/${task##*/}"
aws logs get-log-events --log-group-name /devops-g9/k6 --log-stream-name "$stream" \
  --start-from-head --query 'events[].message' --output text | tr '\t' '\n' >"$OUT/.k6-raw.log"
# Next page(s): get-log-events returns at most 10,000 events or 1 MB per call.
token=""
while :; do
  next=$(aws logs get-log-events --log-group-name /devops-g9/k6 --log-stream-name "$stream" \
    --start-from-head ${token:+--next-token "$token"} --query 'nextForwardToken' --output text)
  [[ "$next" == "$token" || -z "$next" ]] && break
  token="$next"
  aws logs get-log-events --log-group-name /devops-g9/k6 --log-stream-name "$stream" \
    --next-token "$token" --query 'events[].message' --output text | tr '\t' '\n' >>"$OUT/.k6-raw.log"
done

grep -vE 'running \(|\[ *[0-9]+% \]' "$OUT/.k6-raw.log" | sed '/K6_SUMMARY_BEGIN/,/K6_SUMMARY_END/d' >"$OUT/k6-g3-run.txt"
sed -n '/K6_SUMMARY_BEGIN/,/K6_SUMMARY_END/p' "$OUT/.k6-raw.log" | sed '1d;$d' >"$OUT/k6-g3-summary.json"
rm -f "$OUT/.k6-raw.log"

python3 - "$OUT" "$task" "$started" "$stopped" "$exit_code" "$reason" "$running_adapter" <<'PY'
import json, sys
from pathlib import Path
out, task, started, stopped, code, reason, adapter = sys.argv[1:]
meta = {"task": task, "started_utc": started, "stopped_utc": stopped, "k6_exit_code": code,
        "stopped_reason": reason, "payments_adapter": adapter,
        "note": "k6 exit 99 means a threshold was crossed; see k6-g3-summary.json"}
(Path(out) / "k6-g3-meta.json").write_text(json.dumps(meta, indent=2) + "\n")
try:
    m = json.loads((Path(out) / "k6-g3-summary.json").read_text())["metrics"]
    d, f, c, r = m["http_req_duration"], m["http_req_failed"], m["checks"], m["http_reqs"]
    print(f"requests {r['count']} ({r['rate']:.1f}/s)  failed {f['value']:.2%}  "
          f"checks {c['value']:.2%}  p95 {d['p(95)']:.0f} ms  max {d['max']:.0f} ms")
    for name in ("http_req_failed", "http_req_duration", "checks"):
        for rule, crossed in m[name].get("thresholds", {}).items():
            print(("FAIL " if crossed else "PASS ") + f"{name} {rule}")
except (OSError, KeyError, ValueError) as exc:
    print(f"no summary parsed ({exc}); read k6-g3-run.txt")
PY
echo "k6 exit code: $exit_code"
