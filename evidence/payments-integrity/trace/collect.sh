#!/usr/bin/env bash
# One trace id from sale to payment to callback (ADR 0009 section 5).
# Sends one W3C traceparent on a sale's POS calls, lets the fake provider call back, then prints
# every log line from POS and Payments that carries that id. Local services, FakeAdapter only:
#   (cd services/payments && DATABASE_URL=sqlite:////tmp/tr-payments.db python3 app.py > /tmp/tr-payments.log 2>&1) &
#   (cd services/pos && DATABASE_URL=sqlite:////tmp/tr-pos.db PAYMENTS_BASE_URL=http://127.0.0.1:8080 \
#      uvicorn app.main:app --port 8081 > /tmp/tr-pos.log 2>&1) &
#   LOGS="/tmp/tr-pos.log /tmp/tr-payments.log" ./evidence/payments-integrity/trace/collect.sh
# Deployed: the same id in CloudWatch (log groups /devops-g9/pos and /devops-g9/payments).
set -euo pipefail

OUT="$(cd "$(dirname "$0")" && pwd)"
POS="${POS_URL:-http://127.0.0.1:8081}"
PAYMENTS="${PAYMENTS_URL:-http://127.0.0.1:8080}"
LOGS="${LOGS:?set LOGS to the POS and Payments log files}"
PY="${PYTHON:-python3}"

# First 8 hex are the epoch second, the form X-Ray's own trace ids take.
TRACE="$("$PY" -c 'import secrets, time; print(f"{int(time.time()):08x}" + secrets.token_hex(12))')"
TP="00-${TRACE}-$("$PY" -c 'import secrets; print(secrets.token_hex(8))')-01"
post() { curl -sSf -X POST "$1$2" -H 'Content-Type: application/json' -H "traceparent: ${TP}" "${@:4}" --data "$3"; }
field() { "$PY" -c "import json,sys; print(json.load(sys.stdin)['$1'])"; }

TEN="$(post "$POS" /tenants '{"name":"Trace Duka"}' | field id)"
TILL="$(post "$POS" "/tenants/${TEN}/tills" '{"name":"Till 1"}' | field id)"
ATT="$(post "$POS" "/tenants/${TEN}/attendants" '{"name":"Mary","phone":"254000000101","commission_rate_bps":500}' | field id)"
PROD="$(post "$POS" "/tenants/${TEN}/products" '{"sku":"SODA-500","name":"Soda","price_minor":8000,"currency":"KES"}' | field id)"
SALE="$(post "$POS" "/tenants/${TEN}/sales" \
  "{\"till_id\":\"${TILL}\",\"attendant_id\":\"${ATT}\",\"line_items\":[{\"product_id\":\"${PROD}\",\"quantity\":2}]}" \
  -H "Idempotency-Key: trace-${TRACE}" | field id)"
post "$POS" "/tenants/${TEN}/sales/${SALE}/payment-request" '{"phone":"254000000001"}' > /dev/null

# The provider's callback is its own request, with no traceparent of ours.
curl -sSf -X POST "${PAYMENTS}/_fake/advance" -H 'Content-Type: application/json' --data '{"seconds":2}' > /dev/null
curl -sSf -X POST "${PAYMENTS}/_fake/deliver-callbacks" -H 'Content-Type: application/json' --data '{}' > /dev/null
FINAL="$(post "$POS" "/tenants/${TEN}/sales/${SALE}/payment-reconcile" '{}')"
sleep 1

SUMMARISE="$(cat <<'PY'
import json, sys

trace, final = sys.argv[1], json.loads(sys.argv[2])
lines = []
for raw in sys.stdin:
    try:
        lines.append(json.loads(raw))
    except ValueError:
        continue
lines.sort(key=lambda x: x["ts"])
keep = ("ts", "service", "event", "result", "record_kind", "record_id", "state", "trace_id", "origin_trace_id")
events = [{k: x[k] for k in keep if x.get(k) is not None} for x in lines]
results = [x.get("result") or "" for x in lines]
callback = [x for x in lines if x.get("origin_trace_id") == trace and x.get("state") == "SUCCEEDED"]
checks = {
    "pos_logged_the_sale": any("/sales -> 201" in r for r in results),
    "pos_logged_the_payment_request": any("payment-request -> 200" in r for r in results),
    "payments_created_under_the_sale_trace": any(
        x["service"] == "payments" and x.get("result") == "POST /payments -> 201" and x["trace_id"] == trace
        for x in lines
    ),
    "callback_transition_points_back_at_the_sale_trace": len(callback) == 1 and callback[0]["trace_id"] != trace,
    "sale_ended_paid": final.get("status") == "PAID",
}
print(json.dumps({"trace_id": trace, "checks": checks, "all_passed": all(checks.values()), "events": events}, indent=2))
PY
)"
# The program goes in with -c so stdin stays free for the log lines.
# shellcheck disable=SC2086
grep -h "$TRACE" $LOGS | "$PY" -c "$SUMMARISE" "$TRACE" "$FINAL" > "${OUT}/sale-to-callback.json"
"$PY" -c "import json,sys; d=json.load(open('${OUT}/sale-to-callback.json')); print(json.dumps({k: d[k] for k in ('trace_id','checks','all_passed')}, indent=2)); sys.exit(0 if d['all_passed'] else 1)"
