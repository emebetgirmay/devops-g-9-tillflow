# Defence notes: Payments + integrity

For the 6-minute individual defence (`@chesangJ`): design, trade-offs, own PRs, failure behaviour,
proof, then one cross-system scenario. Every claim points at a file a reviewer can open. The
evidence index is [`README.md`](README.md); the G3 code is explained in
[`docs/g3-payments-walkthrough.md`](../../docs/g3-payments-walkthrough.md).

## Design in one minute

- **One owner of money movement.** Only Payments talks to M-Pesa, through a port
  (`services/_shared/mpesa/port.py`). POS and Commission call Payments' HTTP API. Commission holds no
  credentials and imports no M-Pesa code; a guard test fails the build if it does (ADR 0004).
- **State machines with immutable terminal states** (`core/states.py`). A payment goes
  `CREATED → PENDING → SUCCEEDED | DECLINED | EXPIRED`, or to `UNKNOWN → NEEDS_REVIEW` when the
  outcome is not known. A payout is the same with `FAILED`. One function, `Store.transition()`,
  performs every move and refuses an illegal one.
- **Idempotency at three layers.** The caller's `Idempotency-Key` (same key and body returns the
  original, a different body is 409); a server-derived `payout_key` from tenant, attendant and
  period, with a partial unique index allowing one live disbursement per payout; and the ledger's
  `UNIQUE (provider, provider_ref, entry_type)`, so a replayed callback cannot credit twice.
- **Unknown is a state, not an error.** A timeout never becomes a decline and is never retried.
  The sweep moves a silent attempt to `UNKNOWN` after 90 s, reconcile queries the provider after
  120 s with backoff, and after 24 h it becomes `NEEDS_REVIEW` for a human (ADR 0006).

## Trade-offs I chose, and what they cost

| Choice | Why | Cost |
|---|---|---|
| Only documented Daraja result codes are terminal; every other code is `UNKNOWN` | A wrong "failed" on a payment that succeeded loses money silently | More items reach reconcile and review |
| No automatic resubmission of a payout | B2C cannot be reversed through the API | A failed payout waits for an operator |
| Kill switch trips on `CONFIGURATION` or insufficient funds | Repeated bad credentials lock the API user; stop instead of retrying | One bad result pauses every payout until someone resets it |
| Standard library only, SQLite | No dependency to patch, easy to test, deterministic | One task only, no backups, state lost on redeploy; Postgres is the fix (ADR 0002) |
| Pull, not push, to POS | Payments has no outbound HTTP client, which a guard test enforces | POS must poll `payment-reconcile`; a sale can sit unpaid until it does |
| FakeAdapter for CI, k6 and drills | Timeouts and duplicate callbacks cannot be forced in the sandbox | Real Daraja behaviour is proven only by the separate contract test |

## Failure behaviour, with the proof

| Failure | What happens | Proof |
|---|---|---|
| Callback never arrives | `PENDING → UNKNOWN`, reconcile resolves it, same-key retries return the original | `g4/uncertain-payment.json` |
| The initiate call times out | `UNKNOWN` at once; a retry does not call the provider again | `g4/uncertain-payment.json`, last two steps |
| Same callback three times | `applied, replay, replay`, one ledger entry | `g4/callback-replay.json` |
| Contradicting callback after a terminal state | Logged as `illegal_transition`, not applied, pages `payments-critical-anomaly` | `g4/callback-replay.json`; incident 3 in the G3 evidence |
| Commission reruns, or uses a second key | Original returned, or `409 payout_already_requested` | `g4/uncertain-payout.json`, `../commission-payout/checks.json` |
| Provider rejects the configuration | Payout `FAILED`, kill switch trips, next send is `503` | `../daraja-b2c-contract/run3-*.json` |
| Forged callback | Refused by the source allowlist before parsing; the edge overwrites the caller-address header | `../daraja-b2c-contract/run1-*.json`, `tests/test_http.py` |

## What I would say is not done

The list in [`README.md`](README.md#6-not-claimed): no `SUCCEEDED` payout from the real sandbox
yet, no restore because there is no database to restore, Commission not deployed, no single trace
from sale to callback, and the raw Daraja code is not exposed.

The weakest point, said plainly: every guarantee above holds while the database survives. On this
build a redeploy replaces the task and its SQLite file, so idempotency records, the payout keys and
the kill switch are forgotten. A retry after a redeploy could reach the provider a second time.
That is why RDS blocks production, not just the restore drill.

## Cross-system scenarios to practise

Work each one from symptom to cause using only the tools that exist.

1. **"The customer paid but the sale still shows unpaid."** POS only learns the outcome when it
   polls. Check the sale in POS, take its `payment_id`, `GET /payments/{id}` on Payments. If
   `SUCCEEDED`, POS has not reconciled: call the sale's `payment-reconcile`. If `UNKNOWN`, the
   callback was lost: check the scheduled sweep ran (`payments-reconcile-stale` alarm) and the
   oldest-unresolved panel. Never mark it paid by hand.
2. **"An attendant was not paid by 06:30."** Commission's ledger row state first (`PLANNED` means
   never sent, `REQUESTED` means waiting on Payments). Then the payout in Payments. `503
   payouts_disabled` or the `payments-payouts-paused` alarm means the kill switch tripped: find the
   `CONFIGURATION` or insufficient-funds result that tripped it before resetting the flag. Never
   send the payout again by hand.
3. **"`payments-critical-anomaly` paged."** Take the record id from the alarm's log line, filter
   the Payments log group by its `trace_id`, and read the `callback` lines: `replay` is harmless,
   `illegal_transition_logged` means the provider contradicted a terminal state and a human must
   reconcile against the M-PESA portal.
4. **"Payments 5xx after a deploy."** Release smoke fails and ECS rolls back
   (`evidence/platform-delivery/g4-broken-release/`). With the database intact, a client retrying
   with the same key gets the original. On this build the rollback starts a new task with an empty
   database, so say that, and point to RDS.
