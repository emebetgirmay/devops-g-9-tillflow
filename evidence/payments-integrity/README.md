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

## Later
- Traces, k6 results against the fake adapter, sandbox contract proof run by the deployed adapter
