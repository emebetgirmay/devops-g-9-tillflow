#!/usr/bin/env bash
# Build and push the POS health stub to ECR (immutable tag).
set -euo pipefail

TAG="${1:-$(git -C "$(cd "$(dirname "$0")/.." && pwd)" rev-parse HEAD)}"
REGION="${AWS_REGION:-eu-north-1}"
PREFIX="${NAME_PREFIX:-devops-g9}"

AWS_CLI=(aws)
if [ -n "${AWS_PROFILE:-}" ]; then
  AWS_CLI=(aws --profile "$AWS_PROFILE")
fi

ACCOUNT_ID="$("${AWS_CLI[@]}" sts get-caller-identity --query Account --output text)"
REPO="${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com/${PREFIX}/pos"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

echo "Logging in to ECR..."
"${AWS_CLI[@]}" ecr get-login-password --region "$REGION" \
  | docker login --username AWS --password-stdin "${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com"

echo "Building ${REPO}:${TAG} (COMMIT_SHA=${TAG}) ..."
docker build --build-arg "COMMIT_SHA=${TAG}" -t "${REPO}:${TAG}" "$ROOT/services/pos"

echo "Pushing ${REPO}:${TAG} ..."
docker push "${REPO}:${TAG}"

DIGEST="$("${AWS_CLI[@]}" ecr describe-images \
  --repository-name "${PREFIX}/pos" \
  --image-ids "imageTag=${TAG}" \
  --query 'imageDetails[0].imageDigest' \
  --output text)"

echo "Done."
echo "  tag:    ${REPO}:${TAG}"
echo "  digest: ${REPO}@${DIGEST}"
echo "Next: ./scripts/deploy-pos-sha.sh ${TAG}"
