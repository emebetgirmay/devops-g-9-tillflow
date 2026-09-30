#!/usr/bin/env bash
# G4 restore drill (game day scenario 1): point-in-time restore of devops-g9-db to a safe target,
# following docs/runbook.md "Restore" steps 1-3. The live instance and services are not touched.
#
#   1. Markers: a payment and a POS tenant written BEFORE the restore point, then (once RDS can
#      restore past them) a second pair written AFTER it.
#   2. Declare (D). Restore to the latest restorable time T into devops-g9-db-restore.
#   3. Verify inside the VPC as the services' own roles (the one-off db-bootstrap task, overridden):
#      row counts, the "before" markers present, the "after" markers absent.
#   4. RPO = D - T (writes that a disaster at D would lose); RTO = D -> restore verified.
#   5. Delete the restored instance (KEEP_RESTORE=1 to keep it).
#
#   timeline.json, checks.json, verify-log.txt; exits 1 if a check fails.
#
# Creates and deletes one db.t4g.micro. Needs an SSO session: aws sso login --profile g9
#   ./evidence/reliability-ops/g4-restore/restore-drill.sh

set -euo pipefail

export AWS_PROFILE="${AWS_PROFILE:-g9}" AWS_REGION="${AWS_REGION:-eu-north-1}"
BASE="${BASE_URL:-https://nilrqzkq8a.execute-api.eu-north-1.amazonaws.com}"
OUT="$(cd "$(dirname "$0")" && pwd)"

python3 - "$OUT" "$BASE" "${KEEP_RESTORE:-0}" <<'PY'
import json, re, subprocess, sys, time, urllib.error, urllib.request
from datetime import datetime, timezone
from pathlib import Path

out, base, keep = Path(sys.argv[1]), sys.argv[2].rstrip("/"), sys.argv[3] == "1"
SOURCE, TARGET, PREFIX = "devops-g9-db", "devops-g9-db-restore", "devops-g9"
run = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
timeline: dict = {"run": run}


def now():
    return datetime.now(timezone.utc)


def iso(t):
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def save():
    # After every step, so an interrupted drill still leaves its markers and times behind.
    (out / "timeline.json").write_text(json.dumps(timeline, indent=2) + "\n")


def mark(name):
    timeline[name] = iso(now())
    print(f"{timeline[name]}  {name}", flush=True)
    save()


def aws(*args, parse=True):
    r = subprocess.run(["aws", *args, "--output", "json"], capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(f"aws {' '.join(args[:2])} failed: {r.stderr.strip()}")
    return json.loads(r.stdout or "null") if parse else r.stdout


def call(method, path, body=None, key=None):
    req = urllib.request.Request(base + path, method=method, data=None if body is None else json.dumps(body).encode())
    req.add_header("Content-Type", "application/json")
    if key:
        req.add_header("Idempotency-Key", key)
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read() or b"{}")


def markers(tag):
    pay = call("POST", "/payments", {"tenant_id": "restore-drill", "msisdn": "254000000001",
                                      "amount": 150000, "account_reference": f"rd-{tag}"}, f"restore-{run}-{tag}")
    tenant = call("POST", "/tenants", {"name": f"restore-drill-{run}-{tag}"})
    return {"payment_id": pay["payment_id"], "tenant_id": tenant["id"], "written": iso(now())}


def latest_restorable():
    return datetime.fromisoformat(aws("rds", "describe-db-instances", "--db-instance-identifier", SOURCE)
                                  ["DBInstances"][0]["LatestRestorableTime"].replace("Z", "+00:00"))


# 1. Markers around the restore point.
before = markers("before")
print(f"before markers written {before['written']}; waiting until RDS can restore past them")
written_before = datetime.fromisoformat(before["written"].replace("Z", "+00:00"))
while latest_restorable() <= written_before:
    time.sleep(20)
restore_point = latest_restorable()
after = markers("after")
timeline.update({"restore_point": iso(restore_point), "markers": {"before": before, "after": after}})
save()
print(f"restore point {iso(restore_point)}; after markers written {after['written']}")

# 2. Declare and restore.
mark("declared")
declared = now()
db = aws("rds", "describe-db-instances", "--db-instance-identifier", SOURCE)["DBInstances"][0]
aws("rds", "restore-db-instance-to-point-in-time",
    "--source-db-instance-identifier", SOURCE, "--target-db-instance-identifier", TARGET,
    "--restore-time", iso(restore_point),
    "--db-subnet-group-name", db["DBSubnetGroup"]["DBSubnetGroupName"],
    "--db-parameter-group-name", db["DBParameterGroups"][0]["DBParameterGroupName"],
    "--vpc-security-group-ids", *[g["VpcSecurityGroupId"] for g in db["VpcSecurityGroups"]],
    "--no-publicly-accessible", "--no-multi-az",  # PITR of postgres rejects --manage-master-user-password; the logins come with the data
    "--tags", *[f"Key={k},Value={v}" for k, v in {"group": PREFIX, "owner": "emebetgirmay", "environment": "sandbox",
                                                   "service": "platform", "managed-by": "terraform",
                                                   "capstone": "tillflow", "purpose": "g4-restore-drill"}.items()])
mark("restore_started")
subprocess.run(["aws", "rds", "wait", "db-instance-available", "--db-instance-identifier", TARGET], check=True)
mark("restore_available")
host = aws("rds", "describe-db-instances", "--db-instance-identifier", TARGET)["DBInstances"][0]["Endpoint"]["Address"]

# 3. Verify inside the VPC as the services' own roles (their passwords come with the data).
sql_payments = [
    "SELECT 'payments_rows=' || count(*) FROM payments",
    f"SELECT 'payment_before=' || count(*) FROM payments WHERE payment_id = '{before['payment_id']}'",
    f"SELECT 'payment_after=' || count(*) FROM payments WHERE payment_id = '{after['payment_id']}'",
]
sql_pos = [
    "SELECT 'tenants_rows=' || count(*) FROM tenants",
    "SELECT 'sales_rows=' || count(*) FROM sales",
    f"SELECT 'tenant_before=' || count(*) FROM tenants WHERE id = '{before['tenant_id']}'",
    f"SELECT 'tenant_after=' || count(*) FROM tenants WHERE id = '{after['tenant_id']}'",
]


def psql(role, password_env, statements):
    # One -c per statement: with several statements in one -c, psql prints only the last result.
    flags = " ".join(f'-c "{q}"' for q in statements)
    return f'PGUSER={role} PGPASSWORD="${password_env}" psql -X -tA -v ON_ERROR_STOP=1 {flags}'


script = psql("payments", "PAYMENTS_PASSWORD", sql_payments) + " && " + psql("pos", "POS_PASSWORD", sql_pos)
subnets = [s["SubnetId"] for s in aws("ec2", "describe-subnets", "--filters", f"Name=tag:Name,Values={PREFIX}-private-*")["Subnets"]]
sg = aws("ec2", "describe-security-groups", "--filters", f"Name=group-name,Values={PREFIX}-db-bootstrap")["SecurityGroups"][0]["GroupId"]
overrides = {"containerOverrides": [{"name": "psql", "command": [script],
                                     "environment": [{"name": "PGHOST", "value": host}]}]}
task = aws("ecs", "run-task", "--cluster", PREFIX, "--launch-type", "FARGATE",
           "--task-definition", f"{PREFIX}-db-bootstrap", "--overrides", json.dumps(overrides),
           "--network-configuration", json.dumps({"awsvpcConfiguration": {"subnets": subnets, "securityGroups": [sg],
                                                                           "assignPublicIp": "DISABLED"}}))["tasks"][0]["taskArn"]
subprocess.run(["aws", "ecs", "wait", "tasks-stopped", "--cluster", PREFIX, "--tasks", task], check=True)
task_id = task.rsplit("/", 1)[1]
exit_code = aws("ecs", "describe-tasks", "--cluster", PREFIX, "--tasks", task)["tasks"][0]["containers"][0].get("exitCode")
events = aws("logs", "get-log-events", "--log-group-name", f"/{PREFIX}/db-bootstrap",
             "--log-stream-name", f"bootstrap/psql/{task_id}")["events"]
log = "\n".join(e["message"] for e in events)
(out / "verify-log.txt").write_text(log + "\n")
mark("restore_verified")
values = dict(re.findall(r"^(\w+)=(\d+)$", log, re.M))
verified = now()

# 4. RPO / RTO.
timeline.update({
    "restore_point": iso(restore_point),
    "markers": {"before": before, "after": after},
    "rpo_seconds": round((declared - restore_point).total_seconds()),
    "rto_seconds": round((verified - declared).total_seconds()),
})
checks = {
    "verify_task_succeeded": exit_code == 0,
    "payment_before_present": values.get("payment_before") == "1",
    "payment_after_absent": values.get("payment_after") == "0",
    "tenant_before_present": values.get("tenant_before") == "1",
    "tenant_after_absent": values.get("tenant_after") == "0",
    "tables_have_rows": int(values.get("payments_rows", 0)) > 0 and int(values.get("tenants_rows", 0)) > 0,
}

# 5. Clean up.
if not keep:
    aws("rds", "delete-db-instance", "--db-instance-identifier", TARGET, "--skip-final-snapshot",
        "--delete-automated-backups")
    mark("restore_delete_requested")

(out / "timeline.json").write_text(json.dumps(timeline, indent=2) + "\n")
(out / "checks.json").write_text(json.dumps({"run": run, "rows": values, "checks": checks}, indent=2) + "\n")
print(json.dumps({k: timeline[k] for k in ("restore_point", "rpo_seconds", "rto_seconds")}, indent=2))
print("rows:", values)
for name, ok in checks.items():
    print(f"{'PASS' if ok else 'FAIL'}  {name}")
sys.exit(0 if all(checks.values()) else 1)
PY
