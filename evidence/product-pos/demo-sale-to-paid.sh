#!/usr/bin/env bash
# G2 personal proof: end-to-end sale demo against the live sandbox.
#
# Drives the real deployed POS + Payments services (not localhost) through
#   CREATE SALE -> READY_FOR_PAYMENT -> payment-request -> (fake STK) -> PAID
# entirely through the public API Gateway URL — API Gateway proxies every
# path to the internal ALB (infra/envs/sandbox/api_gateway.tf's
# `ANY /{proxy+}` route), which then splits by path: /payments*, /payouts*,
# /_fake*, /_admin* go to Payments, everything else to POS.
#
# `/_fake/*` only exists because MPESA_ADAPTER=fake in this build (ADR
# 0004) — this is exactly the deterministic driver the brief requires for
# CI/k6/demo, standing in for a real Daraja STK callback.
#
# Prereqs: `aws sso login --profile g9` (or whatever profile has sandbox
# access), then run from anywhere:
#   ./evidence/product-pos/demo-sale-to-paid.sh

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
REGION="${AWS_REGION:-eu-north-1}"

AWS_CLI=(aws)
if [ -n "${AWS_PROFILE:-}" ]; then
  AWS_CLI=(aws --profile "$AWS_PROFILE")
fi
export AWS_REGION="$REGION"

cd "${ROOT}/infra/envs/sandbox"
BASE="$(terraform output -raw api_gateway_url)"
cd "${ROOT}"

echo "== base URL: ${BASE} =="

echo "== health (POS only — the ALB's path rules route /payments*, /payouts*,"
echo "   /_fake*, /_admin* to Payments and everything else to POS, so"
echo "   Payments' own bare /health,/ready,/version aren't reachable through"
echo "   this shared entrypoint right now; that's a real, small gap) =="
curl -sfS "${BASE}/health"; echo
curl -sfS "${BASE}/version"; echo

echo
echo "== tenant setup =="
TID=$(curl -sfS -X POST "${BASE}/tenants" -H 'content-type: application/json' \
  -d '{"name":"Demo Duka"}' | python3 -c "import sys,json;print(json.load(sys.stdin)['id'])")
TILL=$(curl -sfS -X POST "${BASE}/tenants/${TID}/tills" -H 'content-type: application/json' \
  -d '{"name":"Till 1"}' | python3 -c "import sys,json;print(json.load(sys.stdin)['id'])")
ATT=$(curl -sfS -X POST "${BASE}/tenants/${TID}/attendants" -H 'content-type: application/json' \
  -d '{"name":"Mary","phone":"254712345678"}' | python3 -c "import sys,json;print(json.load(sys.stdin)['id'])")
PROD=$(curl -sfS -X POST "${BASE}/tenants/${TID}/products" -H 'content-type: application/json' \
  -d '{"sku":"SODA-500","name":"Soda 500ml","price_minor":8000,"currency":"KES"}' \
  | python3 -c "import sys,json;print(json.load(sys.stdin)['id'])")
echo "tenant=${TID} till=${TILL} attendant=${ATT} product=${PROD}"

echo
echo "== create sale (idempotent) =="
SALE=$(curl -sfS -X POST "${BASE}/tenants/${TID}/sales" -H 'content-type: application/json' \
  -H "Idempotency-Key: demo-$(date +%s)" \
  -d "{\"till_id\":\"${TILL}\",\"attendant_id\":\"${ATT}\",\"line_items\":[{\"product_id\":\"${PROD}\",\"quantity\":3}]}")
echo "${SALE}" | python3 -m json.tool
SALE_ID=$(echo "${SALE}" | python3 -c "import sys,json;print(json.load(sys.stdin)['id'])")
TOTAL=$(echo "${SALE}" | python3 -c "import sys,json;print(json.load(sys.stdin)['total_minor'])")
echo "sale=${SALE_ID} status=READY_FOR_PAYMENT total_minor=${TOTAL}"

echo
echo "== payment-request (POS -> Payments STK push) =="
REQ=$(curl -sfS -X POST "${BASE}/tenants/${TID}/sales/${SALE_ID}/payment-request" \
  -H 'content-type: application/json' -d '{"phone":"254712345678"}')
echo "${REQ}" | python3 -m json.tool
PAY_ID=$(echo "${REQ}" | python3 -c "import sys,json;print(json.load(sys.stdin)['payment_id'])")

echo
echo "== drive the deterministic fake STK callback (stands in for Daraja) =="
curl -sfS -X POST "${BASE}/_fake/advance" -d '{"seconds": 30}'; echo
curl -sfS -X POST "${BASE}/_fake/deliver-callbacks" -d '{}'; echo

echo
echo "== POS reconciles (polls Payments — no push exists, see contract doc) =="
RECONCILE=$(curl -sfS -X POST "${BASE}/tenants/${TID}/sales/${SALE_ID}/payment-reconcile")
echo "${RECONCILE}" | python3 -m json.tool

echo
echo "== repeat reconcile: must be a no-op (replay safety) =="
curl -sfS -X POST "${BASE}/tenants/${TID}/sales/${SALE_ID}/payment-reconcile" | python3 -m json.tool

echo
echo "== final sale state =="
curl -sfS "${BASE}/tenants/${TID}/sales/${SALE_ID}" | python3 -m json.tool
