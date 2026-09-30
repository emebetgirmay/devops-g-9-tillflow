#!/usr/bin/env bash
# One X-Ray trace from sale to payment to callback, on the deployed sandbox (FakeAdapter only).
#
# Sends one time-based W3C traceparent on a sale's POS calls through the public URL, lets the fake
# provider call back (a request with no traceparent of ours), then fetches the trace from X-Ray:
#
#   trace.json        aws xray batch-get-traces, as returned
#   waterfall.txt     every segment and subsegment: offset from the sale, duration, service, name
#   checks.json       POS spans, POS -> Payments client span, Payments under it, callback in the
#                     same trace; the script exits 1 if any is missing
#
# Read-only on AWS; writes one test sale in the sandbox. Needs an SSO session:
#   aws sso login --profile g9
#   ./evidence/reliability-ops/xray/collect-trace.sh
# Then open the printed console link for the waterfall screenshot.

set -euo pipefail

export AWS_PROFILE="${AWS_PROFILE:-g9}" AWS_REGION="${AWS_REGION:-eu-north-1}"
BASE="${BASE_URL:-https://ewi66kqbp8.execute-api.eu-north-1.amazonaws.com}"
OUT="$(cd "$(dirname "$0")" && pwd)"

# Time-based trace id: X-Ray rejects ids whose first 8 hex digits are not the current time.
TRACE="$(printf '%08x' "$(date +%s)")$(openssl rand -hex 12)"
TP="00-${TRACE}-$(openssl rand -hex 8)-01"
XRAY_ID="1-${TRACE:0:8}-${TRACE:8}"

post() { curl -sSf -X POST "$BASE$1" -H 'Content-Type: application/json' -H "traceparent: ${TP}" "${@:3}" --data "$2"; }
field() { python3 -c "import json,sys; print(json.load(sys.stdin)['$1'])"; }

echo "trace: $XRAY_ID"
TEN="$(post /tenants '{"name":"X-Ray Trace Duka"}' | field id)"
TILL="$(post "/tenants/${TEN}/tills" '{"name":"Till 1"}' | field id)"
ATT="$(post "/tenants/${TEN}/attendants" '{"name":"Mary","phone":"254000000101","commission_rate_bps":500}' | field id)"
PROD="$(post "/tenants/${TEN}/products" '{"sku":"SODA-500","name":"Soda","price_minor":8000,"currency":"KES"}' | field id)"
SALE="$(post "/tenants/${TEN}/sales" \
  "{\"till_id\":\"${TILL}\",\"attendant_id\":\"${ATT}\",\"line_items\":[{\"product_id\":\"${PROD}\",\"quantity\":2}]}" \
  -H "Idempotency-Key: xray-${TRACE}" | field id)"
post "/tenants/${TEN}/sales/${SALE}/payment-request" '{"phone":"254000000001"}' >/dev/null

# The provider's callback: its own request, with no traceparent of ours.
curl -sSf -X POST "$BASE/_fake/advance" -H 'Content-Type: application/json' --data '{"seconds":2}' >/dev/null
curl -sSf -X POST "$BASE/_fake/deliver-callbacks" -H 'Content-Type: application/json' --data '{}' >/dev/null
post "/tenants/${TEN}/sales/${SALE}/payment-reconcile" '{}' >/dev/null
echo "sale $SALE paid; waiting for X-Ray to index the trace"

for _ in $(seq 1 24); do
  sleep 10
  aws xray batch-get-traces --trace-ids "$XRAY_ID" --output json >"$OUT/trace.json"
  if python3 -c "import json,sys; t=json.load(open(sys.argv[1]))['Traces']; sys.exit(0 if t and len(t[0]['Segments'])>=3 else 1)" "$OUT/trace.json"; then
    break
  fi
done

python3 - "$OUT" "$XRAY_ID" <<'PY'
import json, sys
from pathlib import Path

out, trace_id = Path(sys.argv[1]), sys.argv[2]
traces = json.loads((out / "trace.json").read_text())["Traces"]
if not traces:
    sys.exit(f"trace {trace_id} not found in X-Ray")
docs = [json.loads(s["Document"]) for s in traces[0]["Segments"]]
rows = []

def walk(node, service, depth):
    rows.append((node["start_time"], node.get("end_time", node["start_time"]), service, node["name"], depth,
                 node.get("http", {}).get("response", {}).get("status")))
    for child in node.get("subsegments", []):
        walk(child, service, depth + 1)

for doc in docs:
    walk(doc, doc["name"], 0)
rows.sort()
t0 = rows[0][0]
lines = [f"X-Ray trace {trace_id}", "", f"{'offset':>9} {'duration':>9}  service    span"]
for start, end, service, name, depth, status in rows:
    suffix = f"  [{status}]" if status else ""
    lines.append(f"{(start - t0) * 1000:>7.0f}ms {(end - start) * 1000:>7.0f}ms  {service:<10} {'  ' * depth}{name}{suffix}")
(out / "waterfall.txt").write_text("\n".join(lines) + "\n")
print("\n".join(lines))

names = [(r[2], r[3]) for r in rows]
checks = {
    "pos_request_spans": any(s == "pos" for s, _ in names),
    "pos_to_payments_client_span": any(n.startswith("payments POST /payments") for _, n in names),
    "payments_request_span": any(s == "payments" for s, _ in names),
    "callback_in_the_sale_trace": any(n.endswith("to SUCCEEDED") for _, n in names),
}
(out / "checks.json").write_text(json.dumps({"trace_id": trace_id, "checks": checks}, indent=2) + "\n")
print()
for name, ok in checks.items():
    print(f"{'PASS' if ok else 'FAIL'}  {name}")
print(f"\nconsole: https://eu-north-1.console.aws.amazon.com/cloudwatch/home?region=eu-north-1#xray:traces/{trace_id}")
sys.exit(0 if all(checks.values()) else 1)
PY
