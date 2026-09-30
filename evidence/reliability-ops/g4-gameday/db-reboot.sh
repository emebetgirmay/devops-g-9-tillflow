#!/usr/bin/env bash
# G4 game day, scenario 3 (g4-game-day.md): the database reboots under light traffic.
#
# POS and Payments both answer /ready by pinging RDS, and ECS's container health check and the ALB
# both use /ready. The question: do the services ride out a reboot and reconnect by themselves,
# or does ECS replace them while the database is away?
#
#   1. Record the running POS and Payments tasks; write a payment.
#   2. Probe every 2 s through the public URL: POS /ready, Payments GET /payments/{id}.
#   3. aws rds reboot-db-instance; wait until available; keep probing until both have answered
#      200 for 60 s in a row (or 15 minutes pass).
#   4. Compare the tasks (replaced or not, and why), the invariants, alarm history, RDS events.
#
#   db-reboot-probes.json, db-reboot-timeline.json, db-reboot-checks.json, db-reboot-alarms.json
#
# Interrupts both services briefly: announce it in Slack first. Needs an SSO session.
#   ./evidence/reliability-ops/g4-gameday/db-reboot.sh

set -euo pipefail

export AWS_PROFILE="${AWS_PROFILE:-g9}" AWS_REGION="${AWS_REGION:-eu-north-1}"
BASE="${BASE_URL:-https://nilrqzkq8a.execute-api.eu-north-1.amazonaws.com}"
OUT="$(cd "$(dirname "$0")" && pwd)"

python3 - "$OUT" "$BASE" <<'PY'
import json, subprocess, sys, threading, time, urllib.error, urllib.request
from datetime import datetime, timezone
from pathlib import Path

out, base = Path(sys.argv[1]), sys.argv[2].rstrip("/")
CLUSTER, DB = "devops-g9", "devops-g9-db"
SERVICES = ("devops-g9-pos", "devops-g9-payments")
run = time.strftime("%Y%m%dT%H%M%S", time.gmtime())


def utc(t=None):
    return datetime.fromtimestamp(t if t is not None else time.time(), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def aws(*args):
    r = subprocess.run(["aws", *args, "--output", "json"], capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(f"aws {' '.join(args[:2])} failed: {r.stderr.strip()}")
    return json.loads(r.stdout or "null")


def call(method, path, body=None, key=None):
    req = urllib.request.Request(base + path, method=method, data=None if body is None else json.dumps(body).encode())
    req.add_header("Content-Type", "application/json")
    if key:
        req.add_header("Idempotency-Key", key)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, {}
    except Exception:  # noqa: BLE001 - a timeout or reset is a failed probe, recorded as 0
        return 0, {}


def running_tasks():
    return {s: sorted(aws("ecs", "list-tasks", "--cluster", CLUSTER, "--service-name", s,
                          "--desired-status", "RUNNING")["taskArns"]) for s in SERVICES}


timeline = {"run": run}
tasks_before = running_tasks()
_, pay = call("POST", "/payments", {"tenant_id": "gameday", "msisdn": "254000000001", "amount": 150000,
                                    "account_reference": "gd-reboot"}, f"gameday-{run}-reboot")
payment_path = f"/payments/{pay['payment_id']}"

probes: list[dict] = []
stop = threading.Event()


def probe():
    while not stop.is_set():
        t = time.time()
        probes.append({"t": utc(t), "ts": t, "pos_ready": call("GET", "/ready")[0],
                       "payments_read": call("GET", payment_path)[0]})
        stop.wait(2)


threading.Thread(target=probe, daemon=True).start()
time.sleep(20)  # a baseline before the reboot

timeline["reboot"] = utc()
t_reboot = time.time()
aws("rds", "reboot-db-instance", "--db-instance-identifier", DB)
print(f"{timeline['reboot']}  reboot requested", flush=True)
time.sleep(10)
subprocess.run(["aws", "rds", "wait", "db-instance-available", "--db-instance-identifier", DB], check=True)
timeline["db_available"] = utc()
print(f"{timeline['db_available']}  database available", flush=True)

deadline = time.time() + 900
while time.time() < deadline:
    recent = [p for p in probes if p["ts"] > time.time() - 60]
    # A probe round takes about 3.5 s (two calls plus the pause), so 60 s holds about 17 rounds.
    if len(recent) >= 10 and all(p["pos_ready"] == 200 and p["payments_read"] == 200 for p in recent):
        break
    time.sleep(5)
stop.set()
time.sleep(3)
timeline["stable_60s"] = utc()

failed = {k: [p for p in probes if p[k] != 200 and p["ts"] >= t_reboot] for k in ("pos_ready", "payments_read")}
for k, bad in failed.items():
    timeline[f"{k}_first_failure"] = bad[0]["t"] if bad else None
    timeline[f"{k}_last_failure"] = bad[-1]["t"] if bad else None
    timeline[f"{k}_failed_seconds"] = round(bad[-1]["ts"] - bad[0]["ts"] + 2) if bad else 0

tasks_after = running_tasks()
replaced = {}
for s in SERVICES:
    gone = sorted(set(tasks_before[s]) - set(tasks_after[s]))
    reasons = []
    if gone:
        for t in aws("ecs", "describe-tasks", "--cluster", CLUSTER, "--tasks", *gone)["tasks"]:
            reasons.append({"task": t["taskArn"].rsplit("/", 1)[1], "stopped": t.get("stoppedAt"),
                            "reason": t.get("stoppedReason")})
    replaced[s] = reasons

status, invariants = call("GET", "/_admin/invariants")
events = aws("rds", "describe-events", "--source-identifier", DB, "--source-type", "db-instance",
             "--start-time", timeline["reboot"])["Events"]
alarms = []
for name in ("pos-down", "payments-down", "probe-down", "pos-fast-burn", "payments-fast-burn",
             "alb-5xx-fast-burn", "db-cpu-high", "db-memory-low"):
    for item in aws("cloudwatch", "describe-alarm-history", "--alarm-name", f"devops-g9-{name}",
                    "--history-item-type", "StateUpdate", "--start-date", timeline["reboot"])["AlarmHistoryItems"]:
        alarms.append({"alarm": name, "time": item["Timestamp"], "summary": item["HistorySummary"]})

checks = {
    "recovered_without_intervention": all(p["pos_ready"] == 200 and p["payments_read"] == 200
                                          for p in probes if p["ts"] > time.time() - 60),
    "no_task_replaced": all(not r for r in replaced.values()),
    "payment_intact": call("GET", payment_path)[1].get("payment_id") == pay["payment_id"],
    "invariants_hold": status == 200 and all(v in (True, 0) for k, v in invariants.items()
                                             if k not in ("succeeded_payments", "payment_credits")),
}
(out / "db-reboot-probes.json").write_text(json.dumps([{k: v for k, v in p.items() if k != "ts"} for p in probes]) + "\n")
(out / "db-reboot-timeline.json").write_text(json.dumps({**timeline, "tasks_replaced": replaced,
                                                         "rds_events": [{"t": e["Date"], "m": e["Message"]} for e in events]},
                                                        indent=2, default=str) + "\n")
(out / "db-reboot-alarms.json").write_text(json.dumps(sorted(alarms, key=lambda a: a["time"]), indent=2, default=str) + "\n")
(out / "db-reboot-checks.json").write_text(json.dumps({"run": run, "invariants": invariants, "checks": checks}, indent=2) + "\n")
print(json.dumps({k: v for k, v in timeline.items() if k != "run"}, indent=2))
print("tasks replaced:", json.dumps(replaced, default=str))
print("alarms:", len(alarms))
for name, ok in checks.items():
    print(f"{'PASS' if ok else 'FAIL'}  {name}")
sys.exit(0 if all(checks.values()) else 1)
PY
