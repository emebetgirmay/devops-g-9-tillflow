#!/usr/bin/env bash
# Build and push the POS health stub to ECR (immutable tag).
set -euo pipefail

TAG="${1:-bootstrap}"
REGION="${AWS_REGION:-eu-north-1}"
PREFIX="${NAME_PREFIX:-devops-g9}"
ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
REPO="${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com/${PREFIX}/pos"

ROOT="$(cd "$(dirname "$0")/.." && pwd)"

echo "Logging in to ECR..."
aws ecr get-login-password --region "$REGION" \
  | docker login --username AWS --password-stdin "${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com"

echo "Building ${REPO}:${TAG} ..."
docker build -t "${REPO}:${TAG}" "$ROOT/services/pos"

echo "Pushing ${REPO}:${TAG} ..."
docker push "${REPO}:${TAG}"

echo "Done. Image: ${REPO}:${TAG}"
echo "Next: cd infra/envs/sandbox && terraform apply -var=pos_image_tag=${TAG}"
