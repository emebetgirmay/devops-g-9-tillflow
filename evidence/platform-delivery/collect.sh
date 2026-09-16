#!/usr/bin/env bash
# Collect G1 platform evidence into this directory.
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
terraform output -json > "${OUT}/outputs.json"

HEALTH_URL="$(terraform output -raw health_url)"
READY_URL="$(terraform output -raw ready_url)"
VERSION_URL="$(terraform output -raw version_url)"

curl -sfS "${HEALTH_URL}" | tee "${OUT}/smoke-health.json"
echo
curl -sfS "${READY_URL}" | tee "${OUT}/smoke-ready.json"
echo
curl -sfS "${VERSION_URL}" | tee "${OUT}/smoke-version.json"
echo

CLUSTER="$(terraform output -raw ecs_cluster_name)"
SERVICE="$(terraform output -raw ecs_service_name)"

"${AWS_CLI[@]}" ecs list-tasks \
  --cluster "${CLUSTER}" \
  --service-name "${SERVICE}" \
  --desired-status RUNNING \
  > "${OUT}/ecs-tasks.json"

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
