#!/usr/bin/env bash
# Collect G2 payments-integrity evidence from a running Payments service.
# Fake adapter only: no Safaricom access, no credentials. Start the service first:
#   (cd services/payments && DATABASE_URL=sqlite:////tmp/payments-evidence.db python3 app.py)
#   PAYMENTS_URL=http://127.0.0.1:8080 ./evidence/payments-integrity/collect.sh
set -euo pipefail

OUT="$(cd "$(dirname "$0")" && pwd)"
BASE="${PAYMENTS_URL:-http://127.0.0.1:8080}"
RUN="$(date +%s)"

echo "Collecting into ${OUT} from ${BASE}"

# req METHOD PATH [BODY] [IDEMPOTENCY_KEY] -> body on stdout
req() {
  local method="$1" path="$2" body="${3:-}" key="${4:-}"
  local args=(-sS -X "$method" "${BASE}${path}")
  if [ -n "$body" ]; then args+=(-H 'Content-Type: application/json' --data "$body"); fi
  if [ -n "$key" ]; then args+=(-H "Idempotency-Key: ${key}"); fi
  curl "${args[@]}"
}

# status METHOD PATH BODY KEY -> "HTTP_CODE" on stdout, body to $OUT/$5
status() {
  local method="$1" path="$2" body="$3" key="$4" file="$5"
  curl -sS -o "${OUT}/${file}" -w '%{http_code}' -X "$method" "${BASE}${path}" \
    -H 'Content-Type: application/json' -H "Idempotency-Key: ${key}" --data "$body"
}

field() { python3 -c "import json,sys; print(json.load(sys.stdin)['$1'])"; }

req GET /health  | tee "${OUT}/health.json"; echo
req GET /ready   | tee "${OUT}/ready.json"; echo
req GET /version | tee "${OUT}/version.json"; echo

# 1. Charge round trip: create, callback, one ledger entry.
PAY='{"tenant_id":"evidence","msisdn":"254000000001","amount":150000,"account_reference":"ev-roundtrip"}'
req POST /payments "$PAY" "ev-${RUN}-roundtrip-0001" > "${OUT}/payment-roundtrip-create.json"
ID1="$(field payment_id < "${OUT}/payment-roundtrip-create.json")"
req POST /_fake/advance '{"seconds":2}' > /dev/null
req POST /_fake/deliver-callbacks '{}' > "${OUT}/payment-roundtrip-deliver.json"
req GET "/payments/${ID1}" > "${OUT}/payment-roundtrip-final.json"

# 2. Duplicate callback (delivered 3 times) and a replayed request: one ledger effect.
DUP='{"tenant_id":"evidence","msisdn":"254000000009","amount":150000,"account_reference":"ev-replay"}'
req POST /payments "$DUP" "ev-${RUN}-replay-000001" > "${OUT}/payment-replay-create.json"
ID2="$(field payment_id < "${OUT}/payment-replay-create.json")"
req POST /_fake/advance '{"seconds":2}' > /dev/null
req POST /_fake/deliver-callbacks '{}' > "${OUT}/payment-replay-deliver.json"
req GET "/payments/${ID2}" > "${OUT}/payment-replay-final.json"
REPLAY_HTTP="$(status POST /payments "$DUP" "ev-${RUN}-replay-000001" payment-replay-again.json)"

# 3. Timeout is not a decline: no callback, the sweep moves it to UNKNOWN, never DECLINED.
TMO='{"tenant_id":"evidence","msisdn":"254000000006","amount":150000,"account_reference":"ev-timeout"}'
req POST /payments "$TMO" "ev-${RUN}-timeout-00001" > "${OUT}/payment-timeout-create.json"
ID3="$(field payment_id < "${OUT}/payment-timeout-create.json")"
req POST /_fake/advance '{"seconds":100}' > /dev/null
req POST /_admin/sweep '{}' > "${OUT}/payment-timeout-sweep.json"
req GET "/payments/${ID3}" > "${OUT}/payment-timeout-final.json"

# 4. An unverified result code (1037) resolves to UNKNOWN, not a terminal outcome.
UNV='{"tenant_id":"evidence","msisdn":"254000000006","amount":150000,"account_reference":"ev-code1037"}'
req POST /payments "$UNV" "ev-${RUN}-code1037-0001" > "${OUT}/payment-unverified-create.json"
ID4="$(field payment_id < "${OUT}/payment-unverified-create.json")"
req POST /_fake/script-result-code "{\"kind\":\"payment\",\"id\":\"${ID4}\",\"code\":1037}" > /dev/null
req POST /_fake/deliver-callbacks '{}' > "${OUT}/payment-unverified-deliver.json"
req GET "/payments/${ID4}" > "${OUT}/payment-unverified-final.json"

# 5. Payout: one disbursement, duplicate result, replayed request.
PAYOUT='{"tenant_id":"evidence","attendant_id":"att-1","payout_period":"2026-09-20","msisdn":"254000000109","amount":500000}'
PKEY="ev-${RUN}-payout-000001"
req POST /payouts "$PAYOUT" "$PKEY" > "${OUT}/payout-create.json"
ID5="$(field disbursement_id < "${OUT}/payout-create.json")"
req POST /_fake/advance '{"seconds":2}' > /dev/null
req POST /_fake/deliver-callbacks '{}' > "${OUT}/payout-deliver.json"
req GET "/payouts/${ID5}" > "${OUT}/payout-final.json"
PAYOUT_HTTP="$(status POST /payouts "$PAYOUT" "$PKEY" payout-again.json)"

python3 - "$OUT" "$REPLAY_HTTP" "$PAYOUT_HTTP" <<'PY' > "${OUT}/checks.json"
import json
import sys

out, replay_http, payout_http = sys.argv[1], sys.argv[2], sys.argv[3]


def load(name):
    with open(f"{out}/{name}") as handle:
        return json.load(handle)


roundtrip = load("payment-roundtrip-final.json")
replay = load("payment-replay-final.json")
replay_statuses = [d["status"] for d in load("payment-replay-deliver.json")["delivered"]]
timeout = load("payment-timeout-final.json")
unverified = load("payment-unverified-final.json")
payout = load("payout-final.json")
payout_statuses = [d["status"] for d in load("payout-deliver.json")["delivered"]]

checks = {
    "roundtrip_succeeded_with_one_ledger_entry": (
        roundtrip["state"] == "SUCCEEDED" and roundtrip["ledger_entries"] == 1
    ),
    "duplicate_callback_gives_one_ledger_entry": (
        replay["state"] == "SUCCEEDED"
        and replay["ledger_entries"] == 1
        and replay_statuses == ["applied", "replay", "replay"]
    ),
    "replayed_request_returns_200_not_201": replay_http == "200",
    "timeout_is_unknown_not_a_decline": timeout["state"] == "UNKNOWN",
    "unverified_code_1037_is_unknown_not_terminal": unverified["state"] == "UNKNOWN",
    "payout_succeeded_with_one_ledger_entry": (
        payout["state"] == "SUCCEEDED"
        and payout["ledger_entries"] == 1
        and payout_statuses == ["applied", "replay", "replay"]
    ),
    "replayed_payout_request_returns_200": payout_http == "200",
}
print(json.dumps({"checks": checks, "all_passed": all(checks.values())}, indent=2))
PY
cat "${OUT}/checks.json"

python3 -c "import json,sys; sys.exit(0 if json.load(open('${OUT}/checks.json'))['all_passed'] else 1)"
