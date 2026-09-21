# ADR 0008 - B2C commission payouts

- **Status:** Proposed (draft for G2)
- **Date:** 2026-09-21
- **DRI:** Payments + integrity, Mitingi Joy Chesang (`@chesangJ`)
- **Related:** [ADR 0004](0004-mpesa-adapter.md) (adapter port and sandbox boundary), [ADR 0006](0006-idempotency-replay.md) (keys, timeout and replay rules this ADR reuses), [ADR 0002](0002-rds-postgresql.md) (PostgreSQL), [ADR 0007](0007-multi-tenancy.md) (tenant scope), [architecture](../architecture.md), [SLOs](../slo-error-budgets.md), [threat model](../threat-model.md)
- **Proof:** invariant tests in Commission and Payments driven by the FakeAdapter in `services/_shared/`

## Context

A daily Commission run turns **confirmed paid** sales into payouts to attendants via M-Pesa B2C. The product contract ([architecture](../architecture.md), invariant 4) says replay never double-pays, and the [SLO](../slo-error-budgets.md) makes a duplicate disbursement for one ledger row a P0 with a target of zero. Money leaves the business here, so the dangerous failure is paying twice; being late is the cheaper failure.

Boundaries that already hold and are not reopened: Commission never calls Daraja, only the Payments adapter does ([ADR 0004](0004-mpesa-adapter.md)); a timeout is never a decline and is never retried blindly ([ADR 0006](0006-idempotency-replay.md)); CI and k6 use the FakeAdapter only.

**Source notes.** Written against saved copies of the Daraja portal pages for Getting Started, Reversals and Transaction Status (reviewed 2026-09-21). **The B2C API page itself has not been reviewed.** Anything B2C-specific that those pages do not state is marked "verify" and listed in Open questions. No B2C result code is listed here on purpose.

Documented facts this ADR relies on:

- B2C sends money from the business to a registered mobile money customer. Its API user needs the "ORG B2C API Initiator" role, and the request carries an initiator name and a security credential.
- The security credential is the initiator password encrypted with the M-Pesa public key certificate (RSA, PKCS #1.5 padding, base64). The sandbox and production certificates differ.
- The API is asynchronous: results go to a result URL, and if the callback server is unavailable the gateway "logs a 503 error and discards the results". Callbacks come from a documented list of gateway IP addresses.
- The Transaction Status API can query B2C transactions, keyed by receipt number or `OriginatorConversationID`, and is described as a secondary reconciliation mechanism when callbacks are not received (asynchronous; needs its own API user role).
- A Transaction Status sample result for a B2C payment shows a per-payment charge ("Fee For B2C Payment", a sample value only), which favours one aggregated payout per attendant per day over paying on each sale.

## Decision

### 1. Ownership split

| Concern | Owner | Never |
|---|---|---|
| What is owed: eligibility, amounts, the payout ledger | Commission | Call Daraja or hold Daraja credentials |
| Sending money: disbursement state, adapter call, provider references, reconciliation | Payments | Decide amounts or eligibility |
| Provider access | Payments adapter only ([ADR 0004](0004-mpesa-adapter.md)) | Be reachable from Commission or CI |

Commission asks Payments to disburse through `POST /v1/disbursements` with an `Idempotency-Key`. Commission advances a payout to paid or failed only from the disbursement status Payments reports, never by inference.

### 2. Eligibility and the payout ledger

- **Eligible sales:** only sales whose payment is `SUCCEEDED` ([ADR 0006](0006-idempotency-replay.md)) and that have no successful reversal. Anything else is excluded and stays eligible for a later day if it becomes confirmed.
- **Amounts:** integer minor units, computed by Commission from rules owned by Product (Open questions).
- **One payout per attendant per business day:** `payout_ledger` has `UNIQUE (tenant_id, attendant_id, business_date)`. The business date is an EAT calendar day.
- **A sale is paid at most once:** `payout_items` has `UNIQUE (sale_id)` linking sales to their payout, so a sale cannot land in two payouts even if the run is replayed or a rule changes.
- **Recipient snapshot:** the payout row stores the MSISDN and amount when it is `PLANNED`. A replay reads the snapshot, so a later change to the attendant record cannot redirect money already planned.
- **Run schedule:** an EventBridge schedule starts the run for the previous business day early enough to leave time for disbursement and reconciliation before the 06:30 EAT SLO deadline. The exact time is an open question. The run message may be delivered more than once (SQS is at-least-once); every write above is constraint-protected, so redelivery changes nothing.

Payout ledger status, advanced by Commission from Payments' reported status:

`PLANNED` (computed, items linked) to `REQUESTED` (disbursement created) to `PAID` or `FAILED` (both terminal). While Payments reports `UNKNOWN` or `NEEDS_REVIEW` the ledger row stays `REQUESTED`.

### 3. Idempotency

- **Key derivation.** Commission derives the key from the payout row, for example `po_` plus the payout row's UUID, so a replayed run produces the **same key**, not a new one. It fits [ADR 0006](0006-idempotency-replay.md) (16 to 64 characters, `[A-Za-z0-9_-]`) with scope `(tenant_id, disbursement.create, key)`.
- **Fingerprint.** Recipient, amount, currency and payout id. Same key with a different payload returns a 409 and sends nothing.
- **Structural guard beyond keys.** A partial unique index allows at most **one disbursement per payout row whose state is not `FAILED`**. Even a stale key replayed after retention cannot create a second live disbursement for the same payout.
- **No automatic resubmission.** A `FAILED` payout is re-queued only by an operator, after checking the previous attempt's provider evidence, and the new attempt gets a new key and a new disbursement. Nothing retries a payout on its own in G2.

### 4. Disbursement state machine (Payments)

Same shape as [ADR 0006](0006-idempotency-replay.md), with `FAILED` in place of the customer-facing `DECLINED` and `EXPIRED` (no customer is prompted).

| State | Meaning | Terminal |
|---|---|---|
| `CREATED` | Intent recorded. The provider call may or may not have been made. | No |
| `PENDING` | Provider accepted the request and returned its identifiers. Waiting for the result. | No |
| `UNKNOWN` | Outcome not known (timeout, timeout notification, missing or late result, crash). Being reconciled. | No |
| `NEEDS_REVIEW` | Reconciliation window elapsed with no definitive answer. A human decides. | No |
| `SUCCEEDED` | Provider confirmed the transfer. Receipt stored; one ledger effect. | Yes |
| `FAILED` | Provider definitively reported that no money moved, with a reason. | Yes |

| # | From | Event | To | Side effect |
|---|---|---|---|---|
| 1 | `CREATED` | Provider accepts the request | `PENDING` | Store `OriginatorConversationID` and `ConversationID` (verify names for B2C) |
| 2 | `CREATED` | Synchronous definitive rejection | `FAILED` | None; alert if the cause is credentials or configuration |
| 3 | `CREATED` | Adapter timeout or crash after the attempt began | `UNKNOWN` | Schedule reconcile; **never resubmit** |
| 4 | `PENDING` | Result callback: success | `SUCCEEDED` | Disbursement ledger entry + outbox event, one transaction |
| 5 | `PENDING` | Result callback: definitive failure | `FAILED` | Store reason and raw code; outbox event |
| 6 | `PENDING` | Timeout notification, or result deadline passed | `UNKNOWN` | Schedule reconcile |
| 7 | `UNKNOWN` | Transaction Status result or late result: success or failure | `SUCCEEDED` / `FAILED` | As rows 4 and 5 |
| 8 | `UNKNOWN` | Query inconclusive or errored | `UNKNOWN` | Next backoff |
| 9 | `UNKNOWN` | Max window elapsed | `NEEDS_REVIEW` | Alert; **not** auto-failed |
| 10 | `NEEDS_REVIEW` | Late result, query, or operator action with provider evidence | `SUCCEEDED` / `FAILED` | As rows 4 and 5; records who and what evidence |

```mermaid
stateDiagram-v2
    [*] --> CREATED
    CREATED --> PENDING: provider accepted
    CREATED --> FAILED: rejected at initiation
    CREATED --> UNKNOWN: timeout or crash, no resubmit
    PENDING --> SUCCEEDED: result success
    PENDING --> FAILED: result failure
    PENDING --> UNKNOWN: timeout notice or deadline
    UNKNOWN --> UNKNOWN: query inconclusive
    UNKNOWN --> SUCCEEDED: query or late result success
    UNKNOWN --> FAILED: query or late result failure
    UNKNOWN --> NEEDS_REVIEW: window elapsed
    NEEDS_REVIEW --> SUCCEEDED: evidence of transfer
    NEEDS_REVIEW --> FAILED: evidence of no transfer
    SUCCEEDED --> [*]
    FAILED --> [*]
```

Terminal states are immutable. An illegal transition is rejected and logged, never applied. A result that contradicts a terminal state is high severity: a `FAILED` disbursement that later reports success means money may have moved, so it raises a P0 alert for manual reconciliation and is never auto-corrected.

### 5. Results, timeouts and reconciliation

- **Result handling** follows [ADR 0006](0006-idempotency-replay.md) section 4: verify the source (documented IP allowlist), then one transaction that locks the disbursement row, checks the transition, inserts the ledger entry with `UNIQUE (provider, originator_conversation_id)`, updates state and inserts the outbox event. Replays return 2xx and change nothing.
- **Dedupe key** is the `OriginatorConversationID`, with the receipt number as a secondary unique reference. Verify that B2C results carry both.
- **A timeout notification is not a failure.** The result callback and the timeout callback are separate URLs in the documented Reversals and Transaction Status APIs; verify the same for B2C. A timeout notification moves the disbursement to `UNKNOWN`, not `FAILED`.
- **Lost callbacks are expected.** The gateway discards results sent while our endpoint is down, so the reconciler is the recovery path, not a rare edge case.
- **Reconcile path:** the Transaction Status API keyed by `OriginatorConversationID`. It is asynchronous, so the reconcile job submits a status request (its own idempotent operation) and waits for its result callback. Read the `TransactionStatus` result parameter, not the query's own `ResultCode` (see [ADR 0006](0006-idempotency-replay.md)).
- **Schedule:** the ADR 0006 backoff, with a 24 h max window before `NEEDS_REVIEW`. An alert fires at 06:30 EAT for any payout not yet terminal.
- **Fail-safe mapping:** any provider code or status we do not recognise leaves the disbursement `UNKNOWN`. B2C result codes are not listed in this ADR until the B2C page is reviewed.

### 6. Money safety controls

- **Credentials.** A dedicated API user with only the B2C initiator role, in its own secret, used only by the Payments adapter. The security credential is produced inside the adapter with the environment's certificate. Nothing in Commission, CI, k6, fixtures or git.
- **Ceilings.** Per-payout and per-run maximums, set in configuration. A payout or run above its ceiling is held for review and not sent.
- **Kill switch.** A Payments configuration flag stops all sends without a deploy. Disbursements created while it is off stay `CREATED` and are picked up when it is re-enabled, never re-sent from scratch (their keys are unchanged).
- **No manual double B2C.** The runbook already says so; operators resolve `NEEDS_REVIEW` with evidence, never by re-sending.
- **Funding.** The business account must hold enough money. An insufficient-funds failure is a definitive `FAILED` for the whole day, not something to retry in a loop (Open questions cover balance monitoring).

### 7. Port and FakeAdapter extension

Follow-up implementation, not part of this ADR's change. Add to `MpesaPort` (and amend [ADR 0004](0004-mpesa-adapter.md)):

- `disburse(request) -> DisbursementAccepted` (takes the idempotency key)
- `query_disbursement_status(originator_conversation_id) -> DisbursementStatus`
- `parse_disbursement_result(headers, body) -> DisbursementEvent`

with the same decline-versus-unknown error split. FakeAdapter magic-recipient scenarios: success, insufficient funds, invalid recipient, timeout with no result, timeout notification then late success, delayed result, duplicate result, and out-of-order results.

### 8. Test and verification plan

Real PostgreSQL, FakeAdapter only.

| # | Invariant | Check |
|---|---|---|
| P1 | Replaying the daily run N times pays each payout once | Payout rows, disbursements and adapter calls do not grow |
| P2 | Concurrent runs or workers pay once | Two parallel runs for one day: one disbursement per payout |
| P3 | A sale is paid at most once | Insert of a second `payout_items` row for a sale fails |
| P4 | Crash between `PLANNED` and `REQUESTED`, and between `CREATED` and the provider call | Restart sends exactly once or leaves `UNKNOWN`; never a second send |
| P5 | Timeout then late success gives one `SUCCEEDED` | No resubmission; one ledger entry |
| P6 | Duplicate and out-of-order results | One effect; contradiction raises the P0 alert and changes nothing |
| P7 | Timeout notification is not a failure | State is `UNKNOWN`, never `FAILED` |
| P8 | Unknown provider code fails safe | Stays `UNKNOWN` |
| P9 | Stale key after retention | The partial unique index still blocks a second live disbursement |
| P10 | Snapshot is honoured | Changing the attendant MSISDN after `PLANNED` does not change the recipient |
| P11 | Reversed or unconfirmed sales are excluded | Not in any payout |
| P12 | Ceilings and kill switch | Over-ceiling payout is held; switch off sends nothing |
| P13 | Sum check | Sum of `SUCCEEDED` disbursements equals the sum of paid payout rows per run |

**k6 outline (FakeAdapter only):** seed many tenants and attendants with confirmed sales; run the close repeatedly and in parallel; inject each fake scenario; then assert by SQL that no payout row has more than one non-`FAILED` disbursement, no sale appears twice, and no `provider_ref` has more than one ledger entry. Thresholds come from the SLO doc once smoke results exist.

## Consequences

- **Duplicate payout is prevented by constraints**, not only by code review: keys derived from the payout row, a unique index on non-failed disbursements, and a unique index on sale ids.
- **Aggregating per attendant per day** reduces per-payment charges and calls, at the cost of attendants being paid the next morning rather than instantly.
- **Some payouts will miss 06:30 EAT.** An `UNKNOWN` payout is not terminal, so it counts against the Commission SLO. Never auto-failing or resubmitting is the accepted price.
- **Manual toil.** `NEEDS_REVIEW` and operator-initiated retries need a runbook entry and an evidence source (the M-PESA Organization Portal, documented as offering transaction management and reconciliation).
- **Extra state and tables** in Payments (`disbursements`, results, outbox) and Commission (`payout_ledger`, `payout_items`), plus storage for raw results.
- **Two independent reconcile paths** exist for B2C (results and Transaction Status), so both must be idempotent against the same constraints.

## Alternatives considered

| Option | Rejected because |
|---|---|
| Commission calls Daraja directly | Forbidden by the brief and [ADR 0004](0004-mpesa-adapter.md); would spread the money-moving credential |
| Pay on every sale in real time | Many small B2C calls, each with its own charge, and no natural daily reconciliation point |
| One B2C call for a whole tenant | B2C targets a single recipient; per-attendant payouts keep each ledger row mapped to one transfer |
| Automatic retry after a failure or timeout | Risks paying twice when the first attempt actually succeeded; only explicit operator retries after evidence |
| Generate a fresh key on every run | Defeats replay safety; the key must derive from the payout row |
| Distributed lock for the daily run | Adds a failure mode; unique constraints and row locks already give the guarantee |
| Dedupe on receipt number only | Absent until success; the originator conversation id exists from acceptance |

## Open questions

| # | Question | Owner |
|---|---|---|
| 1 | Review the B2C API page: request and response fields, command types (the Getting Started terminology lists salary, business and promotion payments), whether the caller supplies an originator id, amount limits, callback shape, result codes, and the names of the result and timeout URLs | `@chesangJ` |
| 2 | Amend [ADR 0004](0004-mpesa-adapter.md) to add the disbursement and Transaction Status methods to `MpesaPort` | `@chesangJ` |
| 3 | Commission rules: rate, rounding, minimum payout and carry-forward, treatment of a reversal that arrives after payout (clawback or netting), and who bears the per-payment B2C charge | `@Moraaalice` |
| 4 | Business day definition (EAT cutoff, tills open across midnight) and the attendant MSISDN source, verification and change controls | `@Moraaalice` |
| 5 | Run schedule time so payouts can reach terminal before 06:30 EAT (EventBridge is Platform's) | `@emebetgirmay` |
| 6 | Align the Commission SLO with payouts left `UNKNOWN` at 06:30 EAT; add runbook entries for the kill switch and `NEEDS_REVIEW` | `@emebetgirmay` |
| 7 | Monitoring and alarm for business account funding (Daraja documents a balance query API role); the alarm is Platform's, the adapter method is Payments' | `@emebetgirmay` |
| 8 | Separate API user and secret for the B2C role (same point as [ADR 0006](0006-idempotency-replay.md) open question 12) | `@emebetgirmay` |
| 9 | Ceiling values and who approves changes to them | `@chesangJ` with `@Moraaalice` |
