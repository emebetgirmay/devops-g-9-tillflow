#!/usr/bin/env bash
# One X-Ray trace from sale to payment to callback, on the deployed sandbox (FakeAdapter only).
#
# Sends one time-based W3C traceparent on a sale's POS calls through the public URL, lets the fake
# provider call back (a request with no traceparent of ours), then fetches the trace from X-Ray
# (spans: ADR 0009, #74):
#
#   trace.json        aws xray batch-get-traces, as returned
#   waterfall.txt     every segment and subsegment: offset from the first, duration, service, name
#   checks.json       POS request span, Payments' span under POS's, the callback in the sale's
#                     trace; the script exits 1 if any is missing
#
# Read-only on AWS; writes one test sale in the sandbox. Needs an SSO session:
#   aws sso login --profile g9
#   ./evidence/reliability-ops/xray/collect-trace.sh
# Then open the printed console link for the waterfall screenshot.
# RENDER_ONLY=1 XRAY_ID=1-... redraws waterfall.txt and checks.json from the saved trace.json.

set -euo pipefail

export AWS_PROFILE="${AWS_PROFILE:-g9}" AWS_REGION="${AWS_REGION:-eu-north-1}"
BASE="${BASE_URL:-https://nilrqzkq8a.execute-api.eu-north-1.amazonaws.com}"
OUT="$(cd "$(dirname "$0")" && pwd)"

if [ "${RENDER_ONLY:-0}" != "1" ]; then
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
fi

python3 - "$OUT" "${XRAY_ID:?}" <<'PY'
import json, sys
from pathlib import Path

out, trace_id = Path(sys.argv[1]), sys.argv[2]
traces = json.loads((out / "trace.json").read_text())["Traces"]
if not traces:
    sys.exit(f"trace {trace_id} not found in X-Ray")
docs = [json.loads(s["Document"]) for s in traces[0]["Segments"]]
rows, service_of = [], {}

def walk(node, service, depth):
    service_of[node["id"]] = service
    route = node.get("metadata", {}).get("default", {}).get("http.route")
    method = node.get("http", {}).get("request", {}).get("method")
    label = f"{method} {route}" if route and method else node["name"]
    rows.append({"start": node["start_time"], "end": node.get("end_time", node["start_time"]),
                 "service": service, "name": label, "depth": depth, "id": node["id"],
                 "parent": node.get("parent_id"),
                 "status": node.get("http", {}).get("response", {}).get("status")})
    for child in node.get("subsegments", []):
        walk(child, service, depth + 1)

for doc in docs:
    # Segments are named after the service; an independent subsegment carries its segment's name
    # only through its parent, so use its own name.
    walk(doc, doc["name"] if doc.get("type") != "subsegment" else "payments", 0)
rows.sort(key=lambda r: r["start"])
t0 = rows[0]["start"]
lines = [f"X-Ray trace {trace_id}", "", f"{'offset':>9} {'duration':>9}  {'service':<9} span"]
for r in rows:
    status = f"  [{r['status']}]" if r["status"] else ""
    lines.append(f"{(r['start'] - t0) * 1000:>7.0f}ms {(r['end'] - r['start']) * 1000:>7.0f}ms  "
                 f"{r['service']:<9} {'  ' * r['depth']}{r['name']}{status}")
(out / "waterfall.txt").write_text("\n".join(lines) + "\n")
print("\n".join(lines))

pos_ids = {r["id"] for r in rows if r["service"] == "pos"}
checks = {
    "pos_request_span": bool(pos_ids),
    "payments_span_under_pos": any(r["service"] == "payments" and r["parent"] in pos_ids for r in rows),
    "callback_in_the_sale_trace": any(r["name"].endswith("to SUCCEEDED") for r in rows),
    "http_status_recorded": any(r["status"] for r in rows),
}
(out / "checks.json").write_text(json.dumps({"trace_id": trace_id, "checks": checks}, indent=2) + "\n")
print()
for name, ok in checks.items():
    print(f"{'PASS' if ok else 'FAIL'}  {name}")
print(f"\nconsole: https://eu-north-1.console.aws.amazon.com/cloudwatch/home?region=eu-north-1#xray:traces/{trace_id}")
sys.exit(0 if all(checks.values()) else 1)
PY
