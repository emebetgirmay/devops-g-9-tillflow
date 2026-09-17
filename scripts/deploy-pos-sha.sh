#!/usr/bin/env bash
# Deploy POS image by immutable tag → resolve digest → new task def → ECS.
set -euo pipefail

TAG="${1:-$(git rev-parse HEAD)}"
REGION="${AWS_REGION:-eu-north-1}"
PREFIX="${NAME_PREFIX:-devops-g9}"
CLUSTER="${PREFIX}"
SERVICE="${PREFIX}-pos"
REPO_NAME="${PREFIX}/pos"

AWS_CLI=(aws)
if [ -n "${AWS_PROFILE:-}" ]; then
  AWS_CLI=(aws --profile "$AWS_PROFILE")
fi

ACCOUNT_ID="$("${AWS_CLI[@]}" sts get-caller-identity --query Account --output text)"
REGISTRY="${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com"

DIGEST="$("${AWS_CLI[@]}" ecr describe-images \
  --repository-name "${REPO_NAME}" \
  --image-ids "imageTag=${TAG}" \
  --query 'imageDetails[0].imageDigest' \
  --output text)"

IMAGE="${REGISTRY}/${REPO_NAME}@${DIGEST}"
echo "Deploying ${IMAGE} (tag ${TAG})"

TASK_ARN="$("${AWS_CLI[@]}" ecs describe-services \
  --cluster "${CLUSTER}" \
  --services "${SERVICE}" \
  --query 'services[0].taskDefinition' \
  --output text)"

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

"${AWS_CLI[@]}" ecs describe-task-definition --task-definition "${TASK_ARN}" \
  --query 'taskDefinition' > "${TMP}/task.json"

python3 - <<PY
import json, os
td = json.load(open("${TMP}/task.json"))
for k in ["taskDefinitionArn", "revision", "status", "requiresAttributes",
          "compatibilities", "registeredAt", "registeredBy", "deregisteredAt"]:
    td.pop(k, None)
image = "${IMAGE}"
sha = "${TAG}"
digest = "${DIGEST}"
for c in td["containerDefinitions"]:
    if c["name"] == "pos":
        c["image"] = image
        env = {e["name"]: e["value"] for e in c.get("environment", [])}
        env["COMMIT_SHA"] = sha
        env["IMAGE_DIGEST"] = digest
        env["PORT"] = env.get("PORT", "8080")
        c["environment"] = [{"name": k, "value": v} for k, v in env.items()]
json.dump(td, open("${TMP}/task-new.json", "w"))
PY

NEW_ARN="$("${AWS_CLI[@]}" ecs register-task-definition \
  --cli-input-json "file://${TMP}/task-new.json" \
  --query 'taskDefinition.taskDefinitionArn' \
  --output text)"

"${AWS_CLI[@]}" ecs update-service \
  --cluster "${CLUSTER}" \
  --service "${SERVICE}" \
  --task-definition "${NEW_ARN}" \
  --desired-count 1 \
  --force-new-deployment >/dev/null

echo "Waiting for service stability..."
"${AWS_CLI[@]}" ecs wait services-stable \
  --cluster "${CLUSTER}" \
  --services "${SERVICE}"

ALB_DNS="${API_GATEWAY_URL:-https://REPLACE-2139590754.eu-north-1.elb.amazonaws.com}"
echo "Smoke:"
curl -sfS "http://${ALB_DNS}/health" | tee /tmp/smoke-health.json; echo
curl -sfS "http://${ALB_DNS}/ready" | tee /tmp/smoke-ready.json; echo
curl -sfS "http://${ALB_DNS}/version" | tee /tmp/smoke-version.json; echo

echo "Deployed task: ${NEW_ARN}"
echo "Re-run: ./evidence/platform-delivery/collect.sh"
