#!/usr/bin/env bash
# Create TillFlow's database roles and schemas on RDS (ADR 0002, infra/envs/sandbox/rds.tf).
#
#   1. For each service (pos, payments, commission) make sure devops-g9/db/<service> holds a
#      login: username, password, host, port, dbname, schema, url, sqlalchemy_url. An existing
#      password is kept unless ROTATE=1. Passwords are generated here and written straight into
#      Secrets Manager from a private temporary file: never on the command line, in Terraform, its
#      state or CI.
#   2. Run the one-off devops-g9-db-bootstrap task inside the VPC. It creates the roles and
#      schemas (idempotent), sets each role's password from its secret, and prints the result.
#
# Run by a person with an SSO session, after the gated apply has created the instance, and again
# after a destroy and rebuild. Safe to re-run.
#
#   aws sso login --profile g9
#   ./infra/scripts/rds-bootstrap.sh
#   ROTATE=1 ./infra/scripts/rds-bootstrap.sh    # new passwords; then redeploy services on RDS

set -euo pipefail

export AWS_PROFILE="${AWS_PROFILE:-g9}" AWS_REGION="${AWS_REGION:-eu-north-1}"
PREFIX=devops-g9
CLUSTER=$PREFIX
DB_NAME=tillflow
ROTATE="${ROTATE:-0}"

umask 077
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

read -r status host port < <(aws rds describe-db-instances --db-instance-identifier "$PREFIX-db" \
  --query 'DBInstances[0].[DBInstanceStatus,Endpoint.Address,Endpoint.Port]' --output text)
[ "$status" = "available" ] || { echo "RDS $PREFIX-db is '$status', not available yet" >&2; exit 1; }
echo "instance: $PREFIX-db ($host:$port) available"

for svc in pos payments commission; do
  secret="$PREFIX/db/$svc"
  current="$TMP/$svc.current.json"
  if [ "$ROTATE" != "1" ] && aws secretsmanager get-secret-value --secret-id "$secret" \
      --query SecretString --output text >"$current" 2>/dev/null; then
    reuse=1
  else
    reuse=0
    : >"$current"
  fi
  python3 - "$svc" "$host" "$port" "$DB_NAME" "$reuse" "$current" >"$TMP/$svc.json" <<'PY'
import json, secrets, sys
svc, host, port, db, reuse, current = sys.argv[1:7]
password = None
if reuse == "1":
    try:
        password = json.load(open(current)).get("password")
    except (ValueError, OSError):
        password = None
password = password or secrets.token_hex(24)  # URL-safe: no escaping needed in the URLs below
base = f"{svc}:{password}@{host}:{port}/{db}?sslmode=require"
print(json.dumps({
    "username": svc, "password": password, "host": host, "port": int(port), "dbname": db,
    "schema": svc,  # also the role's search_path, set by the bootstrap task
    "url": f"postgresql://{base}",
    "sqlalchemy_url": f"postgresql+psycopg://{base}",
}))
PY
  aws secretsmanager put-secret-value --secret-id "$secret" --secret-string "file://$TMP/$svc.json" \
    --query VersionId --output text >/dev/null
  if [ "$reuse" = "1" ] && [ -s "$current" ]; then
    echo "secret: $secret (kept the existing password)"
  else
    echo "secret: $secret (new password)"
  fi
done

subnets=$(aws ec2 describe-subnets --filters "Name=tag:Name,Values=$PREFIX-private-*" \
  --query 'Subnets[].SubnetId' --output text | tr '\t' ',')
sg=$(aws ec2 describe-security-groups --filters "Name=group-name,Values=$PREFIX-db-bootstrap" \
  --query 'SecurityGroups[0].GroupId' --output text)

task=$(aws ecs run-task --cluster "$CLUSTER" --launch-type FARGATE --task-definition "$PREFIX-db-bootstrap" \
  --network-configuration "awsvpcConfiguration={subnets=[$subnets],securityGroups=[$sg],assignPublicIp=DISABLED}" \
  --query 'tasks[0].taskArn' --output text)
echo "bootstrap task: ${task##*/}"
aws ecs wait tasks-stopped --cluster "$CLUSTER" --tasks "$task"

read -r exit_code reason < <(aws ecs describe-tasks --cluster "$CLUSTER" --tasks "$task" \
  --query 'tasks[0].[containers[0].exitCode,stoppedReason]' --output text)
echo "--- task log ---"
aws logs get-log-events --log-group-name "/$PREFIX/db-bootstrap" \
  --log-stream-name "bootstrap/psql/${task##*/}" --query 'events[].message' --output text | tr '\t' '\n'
echo "----------------"
if [ "$exit_code" != "0" ]; then
  echo "bootstrap failed: exit $exit_code ($reason)" >&2
  exit 1
fi
echo "bootstrap done: roles and schemas pos, payments, commission on $DB_NAME"
if [ "$ROTATE" = "1" ]; then
  echo "passwords rotated: redeploy every service with *_database = \"rds\" so it reads the new secret"
fi
