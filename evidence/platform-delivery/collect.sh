#!/usr/bin/env bash
# Collect platform evidence (G1 POS probes plus G2 Payments path) into this directory.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
OUT="$(cd "$(dirname "$0")" && pwd)"
REGION="${AWS_REGION:-eu-north-1}"

AWS_CLI=(aws)
if [ -n "${AWS_PROFILE:-}" ]; then
  AWS_CLI=(aws --profile "$AWS_PROFILE")
fi

export AWS_REGION="$REGION"

echo "Collecting into ${OUT} (region=${REGION})"

cd "${ROOT}/infra/envs/sandbox"
# Write aside, then replace, so a failed terraform output cannot empty the evidence file.
terraform output -json > "${OUT}/outputs.json.tmp"
mv "${OUT}/outputs.json.tmp" "${OUT}/outputs.json"

HEALTH_URL="$(terraform output -raw health_url)"
READY_URL="$(terraform output -raw ready_url)"
VERSION_URL="$(terraform output -raw version_url)"
echo "API: $(terraform output -raw api_gateway_url)"

curl -sfS "${HEALTH_URL}" | tee "${OUT}/smoke-health.json"
echo
curl -sfS "${READY_URL}" | tee "${OUT}/smoke-ready.json"
echo
curl -sfS "${VERSION_URL}" | tee "${OUT}/smoke-version.json"
echo

API_URL="$(terraform output -raw api_gateway_url)"
# Payments is path-routed; /health on the API hits POS. /_admin/sweep is the payments probe.
curl -sfS -X POST "${API_URL}/_admin/sweep" | tee "${OUT}/smoke-payments-sweep.json"
echo

CLUSTER="$(terraform output -raw ecs_cluster_name)"
SERVICE="$(terraform output -raw ecs_service_name)"
PAYMENTS_SERVICE="$(terraform output -raw ecs_payments_service_name)"

"${AWS_CLI[@]}" ecs list-tasks \
  --cluster "${CLUSTER}" \
  --service-name "${SERVICE}" \
  --desired-status RUNNING \
  > "${OUT}/ecs-tasks.json"

"${AWS_CLI[@]}" ecs list-tasks \
  --cluster "${CLUSTER}" \
  --service-name "${PAYMENTS_SERVICE}" \
  --desired-status RUNNING \
  > "${OUT}/ecs-payments-tasks.json"

PAYMENTS_TASK_ARN="$(python3 -c "import json; print(json.load(open('${OUT}/ecs-payments-tasks.json'))['taskArns'][0])")"

"${AWS_CLI[@]}" ecs describe-tasks \
  --cluster "${CLUSTER}" \
  --tasks "${PAYMENTS_TASK_ARN}" \
  > "${OUT}/ecs-payments-task-detail.json"

python3 - <<PY > "${OUT}/ecs-payments-containers.txt"
import json
d = json.load(open("${OUT}/ecs-payments-task-detail.json"))
for c in d["tasks"][0]["containers"]:
    print({
        "name": c.get("name"),
        "lastStatus": c.get("lastStatus"),
        "healthStatus": c.get("healthStatus"),
        "image": c.get("image"),
    })
PY

# POS -> Payments uses ALB :80 from the VPC, not the API Gateway VPC Link.
"${AWS_CLI[@]}" ec2 describe-security-groups \
  --filters Name=group-name,Values=devops-g9-alb,devops-g9-pos \
  > "${OUT}/sg-pos-payments.json"

python3 - <<PY > "${OUT}/sg-pos-payments.txt"
import json
data = json.load(open("${OUT}/sg-pos-payments.json"))
for sg in data.get("SecurityGroups", []):
    print(sg["GroupName"])
    for rule in sg.get("IpPermissions", []):
        if rule.get("FromPort") == 80:
            print("  ingress", rule.get("IpRanges"), rule.get("UserIdGroupPairs"))
    for rule in sg.get("IpPermissionsEgress", []):
        if rule.get("FromPort") == 80:
            print("  egress", rule.get("IpRanges"), rule.get("UserIdGroupPairs"))
PY

TASK_ARN="$(python3 -c "import json; print(json.load(open('${OUT}/ecs-tasks.json'))['taskArns'][0])")"

"${AWS_CLI[@]}" ecs describe-tasks \
  --cluster "${CLUSTER}" \
  --tasks "${TASK_ARN}" \
  > "${OUT}/ecs-task-detail.json"

python3 - <<PY > "${OUT}/ecs-containers.txt"
import json
d = json.load(open("${OUT}/ecs-task-detail.json"))
for c in d["tasks"][0]["containers"]:
    print({
        "name": c.get("name"),
        "lastStatus": c.get("lastStatus"),
        "healthStatus": c.get("healthStatus"),
        "image": c.get("image"),
    })
PY

if ! "${AWS_CLI[@]}" resourcegroupstaggingapi get-resources \
  --tag-filters Key=capstone,Values=tillflow \
  > "${OUT}/tag-audit.json"; then
  echo '{"ResourceTagMappingList":[]}' > "${OUT}/tag-audit.json"
fi

python3 - <<PY > "${OUT}/tag-audit.txt"
import json, collections
data = json.load(open("${OUT}/tag-audit.json"))
tags_seen = collections.Counter()
missing = []
for r in data.get("ResourceTagMappingList", []):
    tags = {t["Key"]: t["Value"] for t in r.get("Tags", [])}
    for req in ["group", "owner", "environment", "service", "managed-by", "capstone"]:
        if req not in tags:
            missing.append((r["ResourceARN"], req))
    for k in tags:
        tags_seen[k] += 1
print("resources tagged capstone=tillflow:", len(data.get("ResourceTagMappingList", [])))
print("tag key coverage:", dict(tags_seen))
if missing:
    print("MISSING REQUIRED TAGS:")
    for arn, req in missing:
        print(f"  {arn}: {req}")
else:
    print("all required tags present (among returned resources)")
PY

echo "Done. Review files in ${OUT}"
ls -la "${OUT}"
