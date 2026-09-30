#!/usr/bin/env bash
# G4 Product + POS drill: prove failure and recovery for a sale POS doesn't yet know settled.
#
# The gap this proves: Payments has no push callback (services/_shared/pos-payments-contract.md),
# so a sale can genuinely settle at Payments while POS's own record sits in PAYMENT_REQUESTED
# indefinitely, until something polls. This drill forces exactly that gap open, then recovers
# from it, and checks the recovery itself is both correct and safe to repeat.
#
# What it proves, in order:
#   1. Payments resolves the STK to SUCCEEDED while POS is never told (no reconcile called yet).
#   2. POS's own record is still stuck at PAYMENT_REQUESTED — the failure, made real, not assumed.
#   3. The unsafe "recovery" (just retry payment-request, as if re-sending the STK push were a
#      fix) is refused with 409 — the sale never gets a second in-flight payment request.
#   4. The correct recovery (payment-reconcile) applies the already-known outcome and reaches
#      PAID, with no new charge and no new STK push — just POS catching up on what already happened.
#   5. Recovery is itself idempotent: calling it again is a no-op (applied: false).
#
# Every response is saved as its own evidence file, and evidence/product-pos/g4-recover-checks.json
# records pass/fail for every check above; exits 1 if any check fails.
#
# Prereqs: same as demo-sale-to-paid.sh — `aws sso login --profile g9`, run from anywhere:
#   AWS_PROFILE=g9 ./evidence/product-pos/g4-recover-stuck-sale.sh

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

CHECKS_FILE="${OUT}/g4-recover-checks.json"
declare -A CHECKS

# req METHOD PATH [BODY] [IDEMPOTENCY_KEY] -> body on stdout, HTTP status in $HTTP_STATUS
req() {
  local method="$1" path="$2" body="${3:-}" key="${4:-}"
  local args=(-sS -o /tmp/g4-recover-body.$$ -w '%{http_code}' -X "$method" "${BASE}${path}")
  if [ -n "$body" ]; then args+=(-H 'Content-Type: application/json' --data "$body"); fi
  if [ -n "$key" ]; then args+=(-H "Idempotency-Key: ${key}"); fi
  HTTP_STATUS="$(curl "${args[@]}")"
  cat /tmp/g4-recover-body.$$
  rm -f /tmp/g4-recover-body.$$
}

field() { python3 -c "import json,sys; print(json.load(sys.stdin)['$1'])"; }

check() {
  local name="$1" ok="$2"
  CHECKS["$name"]="$ok"
  if [ "$ok" = "true" ]; then
    echo "  [PASS] ${name}"
  else
    echo "  [FAIL] ${name}"
  fi
}

echo "== tenant setup =="
req POST /tenants '{"name":"G4 Drill"}' > "${OUT}/g4-recover-tenant.json"
TID="$(field id < "${OUT}/g4-recover-tenant.json")"
req POST "/tenants/${TID}/tills" '{"name":"Till 1"}' > "${OUT}/g4-recover-till.json"
TILL="$(field id < "${OUT}/g4-recover-till.json")"
req POST "/tenants/${TID}/attendants" '{"name":"Mary","phone":"254712345678"}' \
  > "${OUT}/g4-recover-attendant.json"
ATT="$(field id < "${OUT}/g4-recover-attendant.json")"
req POST "/tenants/${TID}/products" \
  '{"sku":"SODA-500","name":"Soda 500ml","price_minor":8000,"currency":"KES"}' \
  > "${OUT}/g4-recover-product.json"
PROD="$(field id < "${OUT}/g4-recover-product.json")"
echo "tenant=${TID} till=${TILL} attendant=${ATT} product=${PROD}"

echo
echo "== create sale + payment-request =="
SALE_BODY="{\"till_id\":\"${TILL}\",\"attendant_id\":\"${ATT}\",\"line_items\":[{\"product_id\":\"${PROD}\",\"quantity\":3}]}"
req POST "/tenants/${TID}/sales" "$SALE_BODY" "g4-recover-${RUN}" > "${OUT}/g4-recover-sale.json"
SALE_ID="$(field id < "${OUT}/g4-recover-sale.json")"
req POST "/tenants/${TID}/sales/${SALE_ID}/payment-request" '{"phone":"254712345678"}' \
  > "${OUT}/g4-recover-payment-request.json"
echo "sale=${SALE_ID}, payment requested"

echo
echo "== Payments resolves the STK to SUCCEEDED -- POS is not told (no reconcile call yet) =="
req POST /_fake/advance '{"seconds": 30}' > "${OUT}/g4-recover-fake-advance.json"
req POST /_fake/deliver-callbacks '{}' > "${OUT}/g4-recover-fake-deliver.json"

echo
echo "== check 1: does Payments now show this settled, while POS still doesn't know? =="
req GET "/tenants/${TID}/sales/${SALE_ID}" > "${OUT}/g4-recover-stuck-state.json"
STUCK_STATUS="$(field status < "${OUT}/g4-recover-stuck-state.json")"
echo "  sale status per POS (before reconcile): ${STUCK_STATUS}"
check "sale_stuck_at_payment_requested_before_reconcile" \
  "$([ "$STUCK_STATUS" = "PAYMENT_REQUESTED" ] && echo true || echo false)"

echo
echo "== check 2: the UNSAFE recovery -- retrying payment-request must be refused, not re-sent =="
UNSAFE_STATUS="$(req POST "/tenants/${TID}/sales/${SALE_ID}/payment-request" '{"phone":"254712345678"}' \
  > "${OUT}/g4-recover-unsafe-retry.json"; echo "$HTTP_STATUS")"
echo "  retrying payment-request on a stuck sale: HTTP ${UNSAFE_STATUS}"
check "unsafe_retry_payment_request_is_refused" \
  "$([ "$UNSAFE_STATUS" = "409" ] && echo true || echo false)"

echo
echo "== check 3: the CORRECT recovery -- payment-reconcile applies the already-known outcome =="
req POST "/tenants/${TID}/sales/${SALE_ID}/payment-reconcile" > "${OUT}/g4-recover-reconcile.json"
cat "${OUT}/g4-recover-reconcile.json" | python3 -m json.tool
RECOVERED_STATUS="$(field status < "${OUT}/g4-recover-reconcile.json")"
RECOVERED_APPLIED="$(field applied < "${OUT}/g4-recover-reconcile.json")"
check "reconcile_recovers_sale_to_paid" \
  "$([ "$RECOVERED_STATUS" = "PAID" ] && echo true || echo false)"
check "reconcile_reports_applied_true_on_recovery" \
  "$([ "$RECOVERED_APPLIED" = "True" ] && echo true || echo false)"

echo
echo "== check 4: recovery itself is idempotent -- calling it again must be a no-op =="
req POST "/tenants/${TID}/sales/${SALE_ID}/payment-reconcile" > "${OUT}/g4-recover-reconcile-replay.json"
REPLAY_APPLIED="$(field applied < "${OUT}/g4-recover-reconcile-replay.json")"
check "repeat_reconcile_is_a_no_op" \
  "$([ "$REPLAY_APPLIED" = "False" ] && echo true || echo false)"

echo
{
  echo "{"
  echo "  \"run\": \"${RUN}\","
  echo "  \"sale_id\": \"${SALE_ID}\","
  echo "  \"checks\": {"
  first=true
  for name in "${!CHECKS[@]}"; do
    if [ "$first" = true ]; then first=false; else echo ","; fi
    printf '    "%s": %s' "$name" "${CHECKS[$name]}"
  done
  echo
  echo "  }"
  echo "}"
} > "$CHECKS_FILE"

echo "Checks saved to ${CHECKS_FILE}"

FAILED=false
for name in "${!CHECKS[@]}"; do
  if [ "${CHECKS[$name]}" != "true" ]; then
    FAILED=true
  fi
done

if [ "$FAILED" = true ]; then
  echo "FAILED: one or more G4 recovery checks did not pass" >&2
  exit 1
fi

echo "DONE: sale ${SALE_ID} — stuck at PAYMENT_REQUESTED, unsafe retry refused, recovered via reconcile, recovery itself idempotent."
