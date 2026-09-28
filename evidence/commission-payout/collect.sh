#!/usr/bin/env bash
# Collect G2 commission-payout evidence: one close.py + disburse.py run against a running
# Payments POST /payouts, then a repeat run, proving the attendant is paid exactly once.
# Fake adapter only: no Safaricom access, no credentials. Start POS and Payments first
# (see README.md), then:
#   PAYMENTS_DB=/tmp/cp-payments.db ./evidence/commission-payout/collect.sh
set -euo pipefail

OUT="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "${OUT}/../.." && pwd)"
PAYMENTS="${PAYMENTS_URL:-http://127.0.0.1:8080}"
POS="${POS_URL:-http://127.0.0.1:8081}"
PAYMENTS_DB="${PAYMENTS_DB:?set PAYMENTS_DB to the sqlite file the Payments service uses}"
COMMISSION_DB="${COMMISSION_DB:-/tmp/cp-commission-$(date +%s).db}"
PY="${PYTHON:-python3}"

echo "Collecting into ${OUT} (POS ${POS}, Payments ${PAYMENTS}, ledger ${COMMISSION_DB})"

post() { curl -sSf -X POST "$1$2" -H 'Content-Type: application/json' "${@:4}" --data "$3"; }
field() { "$PY" -c "import json,sys; print(json.load(sys.stdin)['$1'])"; }

# Commission's own env: its ledger, and the two services it is allowed to call.
export DATABASE_URL="sqlite:///${COMMISSION_DB}" POS_BASE_URL="$POS" PAYMENTS_BASE_URL="$PAYMENTS"
commission() { (cd "${ROOT}/services/commission" && "$PY" "$@"); }

# 1. Seed POS over its own API: one attendant at 5%, three paid sales of 2 x KSh 80.
TENANT="$(post "$POS" /tenants '{"name":"Evidence Duka"}' | field id)"
TILL="$(post "$POS" "/tenants/${TENANT}/tills" '{"name":"Till 1"}' | field id)"
ATT="$(post "$POS" "/tenants/${TENANT}/attendants" \
  '{"name":"Mary","phone":"254000000101","commission_rate_bps":500}' | field id)"
PROD="$(post "$POS" "/tenants/${TENANT}/products" \
  '{"sku":"SODA-500","name":"Soda 500ml","price_minor":8000,"currency":"KES"}' | field id)"
for n in 1 2 3; do
  SALE="$(post "$POS" "/tenants/${TENANT}/sales" \
    "{\"till_id\":\"${TILL}\",\"attendant_id\":\"${ATT}\",\"line_items\":[{\"product_id\":\"${PROD}\",\"quantity\":2}]}" \
    -H "Idempotency-Key: ev-sale-${TENANT}-${n}")"
  SALE_ID="$(field id <<<"$SALE")"
  PAY_ID="$(post "$POS" "/tenants/${TENANT}/sales/${SALE_ID}/payment-request" \
    '{"phone":"254712345678"}' | field payment_id)"
  post "$POS" "/internal/sales/${SALE_ID}/payment-events" \
    "{\"event_id\":\"ev-${SALE_ID}\",\"payment_id\":\"${PAY_ID}\",\"status\":\"PAID\",\"amount_minor\":$(field total_minor <<<"$SALE")}" \
    > /dev/null
done
# Expected: 3 x 2 x 8000 = 48000 minor sales, 5% = 2400 minor (KSh 24) commission.

DAY="$("$PY" -c "from datetime import datetime,timedelta,timezone; print((datetime.now(timezone.utc)+timedelta(hours=3)).date())")"

# 2. First run: close, send, deliver the B2C result, reconcile.
commission close.py --tenant "$TENANT" --business-date "$DAY" | tee "${OUT}/run1-close.json"
commission disburse.py | tee "${OUT}/run1-disburse-send.json"
post "$PAYMENTS" /_fake/advance '{"seconds":2}' > /dev/null
post "$PAYMENTS" /_fake/deliver-callbacks '{}' | tee "${OUT}/run1-b2c-result.json"; echo
# disburse.py's own reconcile saw PENDING and backed off 60s on the wall clock
# (ledger/disburse.py BACKOFF_SECONDS[1]); the next scheduled pass is the one that settles it.
echo "waiting 61s for the reconcile backoff"; sleep 61
commission disburse.py | tee "${OUT}/run1-disburse-reconcile.json"

# 3. Repeat run, same day, same tenant: must change nothing.
commission close.py --tenant "$TENANT" --business-date "$DAY" | tee "${OUT}/run2-close.json"
commission disburse.py | tee "${OUT}/run2-disburse.json"

# 4. State on both sides after both runs.
"$PY" - "$COMMISSION_DB" > "${OUT}/commission-ledger.json" <<'PY'
import json, sqlite3, sys
db = sqlite3.connect(sys.argv[1]); db.row_factory = sqlite3.Row
rows = db.execute("SELECT tenant_id, attendant_id, business_date, amount_minor, state,"
                  " disbursement_id FROM payout_ledger").fetchall()
items = db.execute("SELECT COUNT(*) FROM payout_items").fetchone()[0]
print(json.dumps({"payout_ledger": [dict(r) for r in rows], "payout_items": items}, indent=2))
PY
DISB="$("$PY" -c "import json; print(json.load(open('${OUT}/commission-ledger.json'))['payout_ledger'][0]['disbursement_id'])")"
curl -sSf "${PAYMENTS}/payouts/${DISB}" | tee "${OUT}/payout-final.json"; echo
"$PY" - "$PAYMENTS_DB" "$TENANT" > "${OUT}/payments-db-counts.json" <<'PY'
import json, sqlite3, sys
db, tenant = sqlite3.connect(sys.argv[1]), sys.argv[2]
one = lambda sql: db.execute(sql, (tenant,)).fetchone()[0]
print(json.dumps({
    "disbursements": one("SELECT COUNT(*) FROM disbursements WHERE tenant_id = ?"),
    "disbursement_debits": one(
        "SELECT COUNT(*) FROM ledger_entries WHERE entry_type = 'DISBURSEMENT_DEBIT'"
        " AND tenant_id = ?"),
    "debited_minor": one(
        "SELECT COALESCE(SUM(amount_minor), 0) FROM ledger_entries"
        " WHERE entry_type = 'DISBURSEMENT_DEBIT' AND tenant_id = ?"),
}, indent=2))
PY
cat "${OUT}/payments-db-counts.json"

"$PY" - "$OUT" <<'PY' > "${OUT}/checks.json"
import json, sys
out = sys.argv[1]
load = lambda name: json.load(open(f"{out}/{name}"))
c1, c2 = load("run1-close.json")["tenants"][0], load("run2-close.json")["tenants"][0]
send1, rec1 = load("run1-disburse-send.json"), load("run1-disburse-reconcile.json")
run2 = load("run2-disburse.json")
ledger = load("commission-ledger.json")
payout, counts = load("payout-final.json"), load("payments-db-counts.json")
b2c = [d for d in load("run1-b2c-result.json")["delivered"] if d["kind"] == "b2c"]
checks = {
    "close_computed_one_payout_of_2400_minor": (
        c1["sales_seen"] == 3 and c1["attendants_closed"] == 1
        and [r["amount_minor"] for r in ledger["payout_ledger"]] == [2400]
    ),
    "disburse_sent_it_to_payments_once": send1["send"]["counts"] == {"created": 1},
    "b2c_result_applied": bool(b2c) and b2c[0]["status"] == "applied",
    "reconcile_marked_it_succeeded": rec1["reconcile"]["counts"] == {"succeeded": 1},
    "commission_ledger_row_succeeded": [r["state"] for r in ledger["payout_ledger"]] == ["SUCCEEDED"],
    "payments_payout_succeeded_one_ledger_entry": (
        payout["state"] == "SUCCEEDED" and payout["ledger_entries"] == 1
    ),
    "repeat_close_added_no_payout": c2["attendants_closed"] == 0 and len(ledger["payout_ledger"]) == 1,
    "repeat_disburse_sent_nothing": run2["send"]["checked"] == 0 and run2["reconcile"]["checked"] == 0,
    "paid_exactly_once_in_payments_db": (
        counts["disbursements"] == 1 and counts["disbursement_debits"] == 1
        and counts["debited_minor"] == 2400
    ),
}
print(json.dumps({"checks": checks, "all_passed": all(checks.values())}, indent=2))
PY
cat "${OUT}/checks.json"

"$PY" -c "import json,sys; sys.exit(0 if json.load(open('${OUT}/checks.json'))['all_passed'] else 1)"
