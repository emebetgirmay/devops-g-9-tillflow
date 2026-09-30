#!/usr/bin/env bash
# G5: destroy the whole sandbox and rebuild it from code, timed (runbook "Destroy and rebuild").
#
# Run from a laptop with an SSO session, never from CI: the CI role is in this state and goes too.
# The Terraform state bucket and lock table (infra/bootstrap) stay.
#
#   destroy   set the previous final DB snapshot aside (its name is fixed), terraform destroy
#             (RDS takes a final snapshot first), then list anything still tagged group=devops-g9
#   rebuild   terraform apply, infra/scripts/rds-bootstrap.sh, the Slack webhook (typed, hidden),
#             the release (workflow_dispatch: you approve the sandbox deploy in GitHub), and a
#             smoke test through the new public URL
#
# The rebuilt database is empty: the sandbox runs the FakeAdapter, and restoring data is proven
# separately (G4 restore drill). The final snapshot is kept. The public URL changes (new API id).
#
#   aws sso login --profile g9
#   CONFIRM=destroy-devops-g9 ./infra/scripts/destroy-rebuild.sh            # both phases
#   PHASE=rebuild ./infra/scripts/destroy-rebuild.sh                          # resume a rebuild
#
# Evidence: evidence/platform-delivery/g5-destroy-rebuild/ (timeline.json, leftovers.json, smoke.json)

set -euo pipefail

export AWS_PROFILE="${AWS_PROFILE:-g9}" AWS_REGION="${AWS_REGION:-eu-north-1}"
PHASE="${PHASE:-all}"
PREFIX=devops-g9
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
TF="$ROOT/infra/envs/sandbox"
OUT="$ROOT/evidence/platform-delivery/g5-destroy-rebuild"
mkdir -p "$OUT"
TIMELINE="$OUT/timeline.json"
[ -f "$TIMELINE" ] || echo '{}' >"$TIMELINE"

mark() { # name: record the UTC time of a step in timeline.json
  python3 - "$TIMELINE" "$1" <<'PY'
import json, sys, time
path, name = sys.argv[1], sys.argv[2]
t = json.load(open(path))
t[name] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
json.dump(t, open(path, "w"), indent=2)
print(f"{t[name]}  {name}", flush=True)
PY
}

tf() { terraform -chdir="$TF" "$@"; }

aws sts get-caller-identity --query Arn --output text >/dev/null
if [ "$(gh run list --workflow release.yml --status in_progress --json databaseId --jq 'length')" != "0" ]; then
  echo "a Release run is in progress: wait for it, a destroy would race its apply" >&2
  exit 1
fi
tf init -backend-config=backend.hcl -reconfigure -input=false >/dev/null

if [ "$PHASE" = "all" ] || [ "$PHASE" = "destroy" ]; then
  [ "${CONFIRM:-}" = "destroy-$PREFIX" ] || { echo "set CONFIRM=destroy-$PREFIX to destroy the sandbox" >&2; exit 1; }
  echo '{}' >"$TIMELINE"
  mark start

  # RDS writes its final snapshot under a fixed name; keep an older one by copying it aside.
  if aws rds describe-db-snapshots --db-snapshot-identifier "$PREFIX-db-final" >/dev/null 2>&1; then
    aside="$PREFIX-db-final-$(date -u +%Y%m%d%H%M)"
    aws rds copy-db-snapshot --source-db-snapshot-identifier "$PREFIX-db-final" \
      --target-db-snapshot-identifier "$aside" --copy-tags >/dev/null
    aws rds wait db-snapshot-available --db-snapshot-identifier "$aside"
    aws rds delete-db-snapshot --db-snapshot-identifier "$PREFIX-db-final" >/dev/null
    echo "previous final snapshot kept as $aside"
  fi

  mark destroy_started
  tf destroy -auto-approve -input=false -lock-timeout=5m -no-color | tail -3
  mark destroy_done

  # What is still tagged ours. Expected: the final DB snapshot and the alerts KMS key (pending
  # deletion, 7 days); the tagging API can also list just-deleted resources for a while.
  aws resourcegroupstaggingapi get-resources --tag-filters "Key=group,Values=$PREFIX" \
    --query 'ResourceTagMappingList[].ResourceARN' --output json >"$OUT/leftovers.json"
  python3 -c "import json,sys; a=json.load(open(sys.argv[1])); print(f'tagged group={sys.argv[2]} after destroy: {len(a)}'); [print('  '+x.split(':',5)[-1]) for x in a]" \
    "$OUT/leftovers.json" "$PREFIX"
fi

if [ "$PHASE" = "all" ] || [ "$PHASE" = "rebuild" ]; then
  mark apply_started
  tf apply -auto-approve -input=false -lock-timeout=5m -no-color | tail -3
  mark apply_done

  "$ROOT/infra/scripts/rds-bootstrap.sh"
  mark bootstrap_done

  # The webhook went with its secret (recovery window 0). Typed hidden, written from a private
  # temporary file: never on the command line or in shell history.
  read -rsp "Slack webhook URL (hidden; Enter to skip and set it later): " hook; echo
  if [ -n "$hook" ]; then
    tmp="$(umask 077; mktemp)"
    python3 -c 'import json,sys; print(json.dumps({"url": sys.stdin.read().strip()}))' <<<"$hook" >"$tmp"
    unset hook
    aws secretsmanager put-secret-value --secret-id "$PREFIX/slack-webhook" --secret-string "file://$tmp" >/dev/null
    rm -f "$tmp"
    mark slack_webhook_set
  fi

  # The release's smoke test reads the public URL from this Actions variable; a rebuilt API Gateway
  # has a new id, so point it at the new URL before releasing (else the smoke tests a dead URL).
  # Needs write access to the repository: with several gh accounts, the active one must be the
  # owner's (gh auth switch). On a 403, set it in GitHub (Settings > Secrets and variables >
  # Actions) and press Enter; stopping here would leave a rebuilt sandbox unreleased.
  new_url="$(tf output -raw api_gateway_url)"
  if ! gh variable set API_GATEWAY_URL --body "$new_url"; then
    echo "could not set API_GATEWAY_URL: set it to $new_url in GitHub, then press Enter"
    read -r _
  fi
  mark api_url_variable_set

  if ! gh workflow run release.yml --ref main; then
    echo "could not start the release: run the Release workflow on main in GitHub (Actions), then press Enter"
    read -r _
  fi
  sleep 10
  run_id="$(gh run list --workflow release.yml --limit 1 --json databaseId --jq '.[0].databaseId')"
  echo "release run $run_id: approve the sandbox deployment at"
  echo "  $(gh run view "$run_id" --json url --jq .url)"
  mark release_started
  gh run watch "$run_id" --exit-status --interval 30 >/dev/null
  mark release_done

  base="$(tf output -raw api_gateway_url)"
  echo "public URL: $base"
  python3 - "$base" "$OUT/smoke.json" <<'PY'
import json, sys, time, urllib.request
base, out = sys.argv[1].rstrip("/"), sys.argv[2]

def call(method, path, body=None, key=None):
    req = urllib.request.Request(base + path, method=method, data=None if body is None else json.dumps(body).encode())
    req.add_header("Content-Type", "application/json")
    if key:
        req.add_header("Idempotency-Key", key)
    with urllib.request.urlopen(req, timeout=15) as r:
        return r.status, json.loads(r.read() or b"{}")

run = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
checks = {}
checks["health"] = call("GET", "/health")[0] == 200
checks["ready"] = call("GET", "/ready")[0] == 200
status, tenant = call("POST", "/tenants", {"name": f"rebuild-{run}"})
checks["pos_writes"] = status == 201 and bool(tenant.get("id"))
status, pay = call("POST", "/payments", {"tenant_id": "rebuild", "msisdn": "254000000001", "amount": 150000,
                                         "account_reference": "rebuild"}, f"rebuild-{run}")
checks["payments_writes"] = status == 201
call("POST", "/_fake/advance", {"seconds": 2})
call("POST", "/_fake/deliver-callbacks", {})
checks["payment_settles"] = call("GET", f"/payments/{pay['payment_id']}")[1].get("state") == "SUCCEEDED"
checks["invariants"] = all(v in (True, 0) for k, v in call("GET", "/_admin/invariants")[1].items()
                           if k not in ("succeeded_payments", "payment_credits"))
json.dump({"base_url": base, "checks": checks}, open(out, "w"), indent=2)
for name, ok in checks.items():
    print(f"{'PASS' if ok else 'FAIL'}  {name}")
sys.exit(0 if all(checks.values()) else 1)
PY
  mark smoke_green

  python3 - "$TIMELINE" <<'PY'
import json, sys
from datetime import datetime
t = json.load(open(sys.argv[1]))
ts = {k: datetime.strptime(v, "%Y-%m-%dT%H:%M:%SZ") for k, v in t.items() if isinstance(v, str)}
def span(a, b):
    return round((ts[b] - ts[a]).total_seconds() / 60, 1) if a in ts and b in ts else None
t["minutes"] = {
    "destroy": span("destroy_started", "destroy_done"),
    "apply": span("apply_started", "apply_done"),
    "bootstrap": span("apply_done", "bootstrap_done"),
    "release": span("release_started", "release_done"),
    "rebuild_total": span("apply_started", "smoke_green"),
    "destroy_to_green": span("destroy_started", "smoke_green"),
}
json.dump(t, open(sys.argv[1], "w"), indent=2)
print(json.dumps(t["minutes"], indent=2))
PY
  echo "done. Update README.md and the evidence scripts' BASE_URL default to the new public URL."
fi
