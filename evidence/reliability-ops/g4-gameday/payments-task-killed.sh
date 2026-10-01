#!/usr/bin/env bash
# G4 game day, scenario 2 (g4-game-day.md): the only Payments task is killed. On SQLite this lost
# every payment; on RDS (ADR 0002, #78) nothing may be lost.
#
#   1. Payments' task definition takes DATABASE_URL from its secret, not the SQLite file.
#   2. Writes a settled payment and a pending one through the public URL (FakeAdapter only).
#   3. aws ecs stop-task on the running Payments task; ECS starts a replacement.
#   4. After the service is stable again: both payments read back unchanged, and the same
#      Idempotency-Key replays the original payment instead of creating a second one.
#   5. It leaves the pending payment UNKNOWN for good (the FakeAdapter's memory dies with the task)
#      and says so at the end: clear it afterwards, or payments-payment-unknown-too-long stays on.
#
#   timeline.json   stop, first failed read, first good read, service stable (UTC)
#   checks.json     pass/fail per check; the script exits 1 if any fails
#   alarms.json     payments-down / probe-down state changes in the window
#
# Stops a task: announce it in Slack first. Needs an SSO session: aws sso login --profile g9
#   ./evidence/reliability-ops/g4-gameday/payments-task-killed.sh
#   OUT_PREFIX=rerun- ./evidence/reliability-ops/g4-gameday/payments-task-killed.sh   # keep run 1

set -euo pipefail

export AWS_PROFILE="${AWS_PROFILE:-g9}" AWS_REGION="${AWS_REGION:-eu-north-1}"
BASE="${BASE_URL:-https://nilrqzkq8a.execute-api.eu-north-1.amazonaws.com}"
CLUSTER=devops-g9
SERVICE=devops-g9-payments
OUT="$(cd "$(dirname "$0")" && pwd)"

python3 - "$OUT" "$BASE" "$CLUSTER" "$SERVICE" "${OUT_PREFIX:-}" <<'PY'
import json, subprocess, sys, time, urllib.error, urllib.request
from datetime import datetime, timezone
from pathlib import Path

out, base, cluster, service = Path(sys.argv[1]), sys.argv[2].rstrip("/"), sys.argv[3], sys.argv[4]
prefix = sys.argv[5]  # OUT_PREFIX=rerun- keeps an earlier run's files
run = time.strftime("%Y%m%dT%H%M%S", time.gmtime())


def utc(t=None):
    return datetime.fromtimestamp(t or time.time(), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def aws(*args):
    return json.loads(subprocess.run(["aws", *args, "--output", "json"], check=True,
                                     capture_output=True, text=True).stdout or "null")


def call(method, path, body=None, key=None):
    req = urllib.request.Request(base + path, method=method,
                                 data=None if body is None else json.dumps(body).encode())
    req.add_header("Content-Type", "application/json")
    if key:
        req.add_header("Idempotency-Key", key)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, {}
    except (urllib.error.URLError, TimeoutError):
        return 0, {}


def stk(msisdn, ref):
    return {"tenant_id": "gameday", "msisdn": msisdn, "amount": 150000, "account_reference": ref}


# 1. Where the task's database comes from.
svc = aws("ecs", "describe-services", "--cluster", cluster, "--services", service)["services"][0]
td = aws("ecs", "describe-task-definition", "--task-definition", svc["taskDefinition"])["taskDefinition"]
app = next(c for c in td["containerDefinitions"] if c["name"] == "payments")
db_secret = next((s["valueFrom"] for s in app.get("secrets", []) if s["name"] == "DATABASE_URL"), "")
db_env = next((e["value"] for e in app.get("environment", []) if e["name"] == "DATABASE_URL"), None)
print(f"task definition {svc['taskDefinition'].rsplit('/', 1)[1]}: DATABASE_URL from "
      f"{'secret ' + db_secret.split(':secret:')[-1] if db_secret else 'environment ' + str(db_env)}")

# 2. State before.
keys = {"paid": f"gameday-{run}-paid", "pending": f"gameday-{run}-pending"}
_, paid = call("POST", "/payments", stk("254000000001", "gd-paid"), keys["paid"])
_, pending = call("POST", "/payments", stk("254000000006", "gd-pending"), keys["pending"])
call("POST", "/_fake/advance", {"seconds": 2})
call("POST", "/_fake/deliver-callbacks", {})
before = {name: call("GET", f"/payments/{p['payment_id']}")[1] for name, p in (("paid", paid), ("pending", pending))}
print("before:", {k: v.get("state") for k, v in before.items()})

# 3. Kill the running task.
(task_arn,) = aws("ecs", "list-tasks", "--cluster", cluster, "--service-name", service,
                  "--desired-status", "RUNNING")["taskArns"][:1]
timeline = {"stopped_task": task_arn.rsplit("/", 1)[1], "stop": utc()}
t_stop = time.time()
aws("ecs", "stop-task", "--cluster", cluster, "--task", task_arn, "--reason", f"G4 game day {run}: task killed")
print(f"stopped {timeline['stopped_task']} at {timeline['stop']}")

first_fail = first_ok = None
deadline = time.time() + 900
while time.time() < deadline:
    status, _ = call("GET", f"/payments/{paid['payment_id']}")
    now = time.time()
    if status != 200 and first_fail is None:
        first_fail = now
    if status == 200 and first_fail is not None:
        running = aws("ecs", "list-tasks", "--cluster", cluster, "--service-name", service,
                      "--desired-status", "RUNNING")["taskArns"]
        if running and task_arn not in running:
            first_ok = now
            break
    time.sleep(5)
subprocess.run(["aws", "ecs", "wait", "services-stable", "--cluster", cluster, "--services", service], check=True)
timeline.update({
    "first_failed_read": utc(first_fail) if first_fail else None,
    "first_good_read_on_new_task": utc(first_ok) if first_ok else None,
    "service_stable": utc(),
    "unavailable_seconds": round(first_ok - first_fail) if first_ok and first_fail else None,
    "recovered_seconds_after_stop": round(first_ok - t_stop) if first_ok else None,
})

# 4. State after.
after = {name: call("GET", f"/payments/{p['payment_id']}")[1] for name, p in (("paid", paid), ("pending", pending))}
replay_status, replay = call("POST", "/payments", stk("254000000001", "gd-paid"), keys["paid"])
print("after: ", {k: v.get("state") for k, v in after.items()})

checks = {
    "database_url_from_secret": bool(db_secret) and db_env is None,
    "replacement_task_served": first_ok is not None,
    "settled_payment_survived": after["paid"].get("state") == before["paid"].get("state") == "SUCCEEDED",
    "pending_payment_survived": bool(after["pending"].get("payment_id")) and
                                after["pending"].get("payment_id") == before["pending"].get("payment_id"),
    "idempotency_key_survived": replay.get("payment_id") == paid.get("payment_id"),
}
alarms = []
for name in ("devops-g9-payments-down", "devops-g9-probe-down"):
    for item in aws("cloudwatch", "describe-alarm-history", "--alarm-name", name, "--history-item-type",
                    "StateUpdate", "--start-date", timeline["stop"])["AlarmHistoryItems"]:
        alarms.append({"alarm": name, "time": item["Timestamp"], "summary": item["HistorySummary"]})

(out / f"{prefix}timeline.json").write_text(json.dumps(timeline, indent=2) + "\n")
(out / f"{prefix}alarms.json").write_text(json.dumps(sorted(alarms, key=lambda a: a["time"]), indent=2) + "\n")
(out / f"{prefix}checks.json").write_text(json.dumps({"run": run, "payments": {k: v.get("payment_id") for k, v in after.items()},
                                            "before": {k: v.get("state") for k, v in before.items()},
                                            "after": {k: v.get("state") for k, v in after.items()},
                                            "replay_http": replay_status, "checks": checks}, indent=2) + "\n")
print(json.dumps(timeline, indent=2))
for name, ok in checks.items():
    print(f"{'PASS' if ok else 'FAIL'}  {name}")
print(f"alarms in window: {len(alarms)} (re-run collection later: payments-down needs 2 minutes to fire)")
# The pending payment cannot settle: the FakeAdapter keeps its "provider" in the killed task's
# memory, so it stays UNKNOWN and payments-payment-unknown-too-long fires after 10 minutes (as it
# should). Clear it once the evidence is collected (scar log, 2026-10-01).
if after["pending"].get("state") in ("PENDING", "UNKNOWN"):
    print(f"\nLEFT IN FLIGHT: {after['pending'].get('payment_id')} ({after['pending'].get('state')}). "
          "Move it UNKNOWN -> EXPIRED with an anomaly row once the drill is written up "
          "(see docs/scar-log.md, 2026-10-01), or the unknown-too-long alarm stays on.")
sys.exit(0 if all(checks.values()) else 1)
PY
