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
# Every response is saved under this directory (matching the convention
# evidence/payments-integrity/collect.sh uses) so this is reproducible
# evidence, not just terminal output.
#
# Prereqs: `aws sso login --profile g9` (or whatever profile has sandbox
# access), then run from anywhere:
#   AWS_PROFILE=g9 ./evidence/product-pos/demo-sale-to-paid.sh

set -euo pipefail

OUT="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "${OUT}/../.." && pwd)"
REGION="${AWS_REGION:-eu-north-1}"
RUN="$(date +%s)"

AWS_CLI=(aws)
if [ -n "${AWS_PROFILE:-}" ]; then
  AWS_CLI=(aws --profile "$AWS_PROFILE")
fi
export AWS_REGION="$REGION"

cd "${ROOT}/infra/envs/sandbox"
BASE="$(terraform output -raw api_gateway_url)"
cd "${ROOT}"

echo "Collecting into ${OUT} (base URL: ${BASE})"

# req METHOD PATH [BODY] [IDEMPOTENCY_KEY] -> body on stdout
req() {
  local method="$1" path="$2" body="${3:-}" key="${4:-}"
  local args=(-sS -X "$method" "${BASE}${path}")
  if [ -n "$body" ]; then args+=(-H 'Content-Type: application/json' --data "$body"); fi
  if [ -n "$key" ]; then args+=(-H "Idempotency-Key: ${key}"); fi
  curl "${args[@]}"
}

field() { python3 -c "import json,sys; print(json.load(sys.stdin)['$1'])"; }

echo "== health (POS only — the ALB's path rules route /payments*, /payouts*,"
echo "   /_fake*, /_admin* to Payments and everything else to POS, so"
echo "   Payments' own bare /health,/ready,/version aren't reachable through"
echo "   this shared entrypoint right now; that's a real, small gap) =="
req GET /health  | tee "${OUT}/sale-demo-health.json"; echo
req GET /version | tee "${OUT}/sale-demo-version.json"; echo

echo
echo "== tenant setup =="
req POST /tenants '{"name":"Demo Duka"}' > "${OUT}/sale-demo-tenant.json"
TID="$(field id < "${OUT}/sale-demo-tenant.json")"

req POST "/tenants/${TID}/tills" '{"name":"Till 1"}' > "${OUT}/sale-demo-till.json"
TILL="$(field id < "${OUT}/sale-demo-till.json")"

req POST "/tenants/${TID}/attendants" '{"name":"Mary","phone":"254712345678"}' \
  > "${OUT}/sale-demo-attendant.json"
ATT="$(field id < "${OUT}/sale-demo-attendant.json")"

req POST "/tenants/${TID}/products" \
  '{"sku":"SODA-500","name":"Soda 500ml","price_minor":8000,"currency":"KES"}' \
  > "${OUT}/sale-demo-product.json"
PROD="$(field id < "${OUT}/sale-demo-product.json")"
echo "tenant=${TID} till=${TILL} attendant=${ATT} product=${PROD}"

echo
echo "== create sale (idempotent) =="
SALE_BODY="{\"till_id\":\"${TILL}\",\"attendant_id\":\"${ATT}\",\"line_items\":[{\"product_id\":\"${PROD}\",\"quantity\":3}]}"
req POST "/tenants/${TID}/sales" "$SALE_BODY" "sale-demo-${RUN}" > "${OUT}/sale-demo-create.json"
cat "${OUT}/sale-demo-create.json" | python3 -m json.tool
SALE_ID="$(field id < "${OUT}/sale-demo-create.json")"
TOTAL="$(field total_minor < "${OUT}/sale-demo-create.json")"
echo "sale=${SALE_ID} status=READY_FOR_PAYMENT total_minor=${TOTAL}"

echo
echo "== payment-request (POS -> Payments STK push) =="
req POST "/tenants/${TID}/sales/${SALE_ID}/payment-request" '{"phone":"254712345678"}' \
  > "${OUT}/sale-demo-payment-request.json"
cat "${OUT}/sale-demo-payment-request.json" | python3 -m json.tool

echo
echo "== drive the deterministic fake STK callback (stands in for Daraja) =="
req POST /_fake/advance '{"seconds": 30}' > "${OUT}/sale-demo-fake-advance.json"
req POST /_fake/deliver-callbacks '{}' > "${OUT}/sale-demo-fake-deliver.json"
cat "${OUT}/sale-demo-fake-deliver.json" | python3 -m json.tool

echo
echo "== payment-reconcile — POS polls Payments (no push exists, see contract"
echo "   doc); loop until the sale reaches PAID or we give up =="
STATUS="PAYMENT_REQUESTED"
for attempt in 1 2 3 4 5; do
  req POST "/tenants/${TID}/sales/${SALE_ID}/payment-reconcile" \
    > "${OUT}/sale-demo-reconcile-${attempt}.json"
  cat "${OUT}/sale-demo-reconcile-${attempt}.json" | python3 -m json.tool
  STATUS="$(field status < "${OUT}/sale-demo-reconcile-${attempt}.json")"
  if [ "$STATUS" = "PAID" ]; then
    break
  fi
  echo "  (status=${STATUS}, retrying...)"
  sleep 2
done

if [ "$STATUS" != "PAID" ]; then
  echo "FAILED: sale did not reach PAID after 5 reconcile attempts (status=${STATUS})" >&2
  exit 1
fi

echo
echo "== repeat reconcile once more: must be a no-op (replay safety) =="
req POST "/tenants/${TID}/sales/${SALE_ID}/payment-reconcile" > "${OUT}/sale-demo-reconcile-replay.json"
cat "${OUT}/sale-demo-reconcile-replay.json" | python3 -m json.tool

echo
echo "== final sale state =="
req GET "/tenants/${TID}/sales/${SALE_ID}" | tee "${OUT}/sale-demo-final.json" | python3 -m json.tool

echo
echo "DONE: sale ${SALE_ID} reached PAID. Evidence saved under ${OUT}/sale-demo-*.json"
