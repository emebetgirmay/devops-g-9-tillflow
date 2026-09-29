# Evidence — Payments + integrity

DRI: Mitingi Joy Chesang (`@chesangJ`)

## G0
- [ ] ADR 0004 accepted
- [ ] Threat-model money section reviewed

## G2 — invariants against the running service (fake adapter only)

`collect.sh` drives a running Payments service and writes JSON evidence next to it. It never
reaches Safaricom and needs no credentials.

```bash
(cd services/payments && DATABASE_URL=sqlite:////tmp/payments-evidence.db python3 app.py) &
PAYMENTS_URL=http://127.0.0.1:8080 ./evidence/payments-integrity/collect.sh
```

| File | Shows |
|---|---|
| `health.json`, `ready.json`, `version.json` | Probes, same shape as `services/pos` |
| `payment-roundtrip-*.json` | Create, callback, `SUCCEEDED`, one ledger entry |
| `payment-replay-*.json` | A callback delivered three times (`applied`, `replay`, `replay`) and a replayed request (200): still one ledger entry |
| `payment-timeout-*.json` | No callback: the sweep moves it to `UNKNOWN`, never `DECLINED` |
| `payment-unverified-*.json` | Result code 1037 (unverified) resolves to `UNKNOWN`, not a terminal outcome |
| `payout-*.json` | A B2C payout with a duplicate result and a replayed request: one ledger entry |
| `checks.json` | Pass or fail for each invariant above; `collect.sh` exits non-zero if any fails |

The test suites that prove the same properties run without a server:

```bash
(cd services/_shared && python3 -m unittest discover -s tests -t .)
(cd services/payments && python3 -m unittest discover -s tests)
(cd services/commission && python3 -m unittest discover -s tests)
```

## G4 — failure drills, executed and timed

`g4/drills.py` (stdlib only) runs the brief's Payments drills against a running service on the
FakeAdapter; `/_fake/*` answers 404 under the Daraja adapter, so it cannot touch Safaricom.

```bash
PAYMENTS_URL=https://<api endpoint> python3 evidence/payments-integrity/g4/drills.py
```

| Drill | Forced failure | Must hold |
|---|---|---|
| `uncertain-payment` | STK callback never arrives (254000000007); the initiate call itself times out (254000000012) | Sweep moves it to `UNKNOWN`, never `DECLINED`; same-key retries while pending, while `UNKNOWN` and after recovery return the original (the fake refuses a second initiate for a key); reconcile by status query ends `SUCCEEDED` with one ledger entry |
| `uncertain-payout` | B2C result never arrives (254000000107) | `UNKNOWN`, never `FAILED`; Commission's same-key rerun returns the original, a second key gets `409 payout_already_requested`; reconcile ends `SUCCEEDED`, one ledger entry |
| `callback-replay` | Same callback 3x (254000000009, 254000000109); success then failure (254000000010, 254000000110); failure then success (254000000011) | Verdicts `applied, replay, replay` and `applied, illegal_transition_logged`; one ledger effect; a terminal state never flips; each delivery ran under its own trace id (`trace_ids` in `checks.json`) |

Each step records `wall_s` (real time the drill took) and `provider_s` (the service's manual
provider clock, advanced past the 90 s callback deadline and 120 s reconcile SLA without waiting).
A trace explains a duplicate on its own: every delivery logs `{"event": "callback", "result":
"replay"}` under the delivery's `trace_id`, and anomalies carry the same `trace_id`
(`services/payments/tests/test_trace_evidence.py`). Look one up with:

```bash
aws logs filter-log-events --log-group-name /devops-g9/payments --filter-pattern '"<trace_id>"'
```

## Later
- Traces, k6 results against the fake adapter, sandbox contract proof run by the deployed adapter
