# G2/G3 POS walkthrough — defense prep for Alice

This is the companion to `docs/g3-payments-walkthrough.md`, but for `services/pos`: the sale
flow, idempotency/conflict handling, the reconcile scheduler, and ADR 0010's `GET /metrics` work
item (P-1). Written to be narrated live, not just read beforehand.

**Scope: this covers the G2 product flow (sale → payment-request → paid) and ADR 0010's P-1.**
Grafana/alerts/probe/EventBridge/the ADOT scrape wiring are Emebet's (ADR 0010, not POS's). The
web frontend is explicitly out of scope for this capstone — see ADR 0010's Product decision on
open question 1 — so there's nothing to defend there either; say so plainly if asked.

## The one-sentence version

POS owns the sale from creation through settlement — it prices sales server-side, never trusts a
client-sent total, and treats every payment outcome (on-demand or polled in the background) as an
event that can arrive twice or out of order without ever double-applying it.

## 1. The product flow and why pricing is server-side — `app/routers/sales.py::create_sale`

**What it is:** `CREATE SALE -> READY_FOR_PAYMENT -> (Payments STK) -> PAID`. The client sends
only `till_id`, `attendant_id`, and `line_items: [{product_id, quantity}]` — never a price or a
total. `create_sale` looks up every product from the tenant's own catalog and computes
`total_minor` itself (`services/pos/app/routers/sales.py:79-106`).

**Why, if asked:** a client-supplied total is a trust boundary a POS terminal can't be allowed to
cross — a compromised or buggy till app could charge a customer 8000 and tell POS the total was 1.
Computing it server-side from `Product.price_minor` means the number that goes to Payments (and
gets charged to a real phone) can never be anything other than what the tenant's own catalog says
it should be.

**Demo it live:**
```bash
cd services/pos && python3 -m uvicorn app.main:app --port 8080 &
TID=$(curl -s -X POST localhost:8080/tenants -d '{"name":"Demo"}' -H 'content-type: application/json' | python3 -c 'import json,sys;print(json.load(sys.stdin)["id"])')
# ...till/attendant/product setup, see evidence/product-pos/demo-sale-to-paid.sh for the full script
```
Point at the response: `total_minor` is present and correct even though the request body never
sent one.

## 2. Idempotent sale creation — `_find_sale_by_idempotency_key`, `_payload_matches_existing`

**What it is:** `POST /tenants/{tenant_id}/sales` requires an `Idempotency-Key` header. A repeat
with the *same* key and the *same* payload (till, attendant, line items) returns the original sale
(`"replayed"`). A repeat with the same key but a *different* payload is a 409 conflict, not a
silent overwrite (`services/pos/app/routers/sales.py:56-64`).

**The bug worth mentioning if asked "was this always this careful":** no — the first version
matched purely on the key and returned the existing sale regardless of payload. Found while adding
the `pos_sale_creates_total{result="conflict"}` metric (a metric you can't write correctly forces
you to name every real outcome, which is exactly how this gap surfaced), fixed by adding
`_payload_matches_existing()`. `tests/test_sale_idempotency.py` covers it directly.

**Why the race matters, not just the happy path:** two concurrent retries with the same key can
both pass the `SELECT` lookup before either commits. `create_sale` handles that too — the `INSERT`
races on a real unique constraint `(tenant_id, idempotency_key)`, and the `except IntegrityError`
branch re-queries and applies the exact same replay-vs-conflict logic to whichever row actually won
(`services/pos/app/routers/sales.py:119-136`). Without this, the loser of the race would raise an
unhandled 500 instead of correctly recognizing its own retry.

**Demo it live:** run the three-request sequence in `tests/test_metrics.py::test_sale_create_metrics_by_result`
manually with curl — same key/same body twice, then same key/different quantity — and show 201,
201 (identical sale id both times), then 409.

## 3. The sale state machine and payment-event replay safety — `app/state.py`, `app/payment_outcomes.py`

**What it is:** `SaleStatus` plus an explicit legal-transition table (`app/state.py:25-31`). No
same-state shortcut — `PAYMENT_REQUESTED -> PAYMENT_REQUESTED` raises, because a second payment
request while one is already in flight for the same sale is a real conflict (accidentally
double-triggering an STK push), not a no-op.

**The design decision worth explaining: replay safety lives one level above the state machine, not
inside it.** `transition()` itself is strict — anything not in the table raises. `apply_outcome`
(`app/payment_outcomes.py:37-90`) is what callers actually use for a *payment event* (as opposed to
a direct user action): it dedupes on `event_id` first (a `PaymentEvent` row, unique — the same
callback delivered twice is a pure no-op, `result="replay"`), and if the event is new but the
transition is illegal for the sale's *current* state (e.g. a stale `DECLINED` verdict polled after
the sale already reached `PAID` via an earlier poll), it still records the event for audit but
never applies it (`result="rejected"`) — it catches `InvalidTransition` rather than letting the
strict state machine's exception propagate as an error. That split is what makes "a timeout is not
a decline" hold: a non-terminal Payments state is simply not in `PAYMENTS_TERMINAL_STATE_MAP` at
all, so `reconcile_sale` returns a no-op rather than ever mapping "still pending" onto a failure.

**Why this had to be shared code, not duplicated:** the same outcome can arrive from two different
callers — the on-demand `POST .../payment-reconcile` endpoint and the background scheduler (both
call `reconcile_sale`, which calls `apply_outcome`). If replay/reorder handling lived separately in
each caller, they could drift and disagree about whether the same event had already been applied.

**Demo it live:** reuse `tests/test_payment_events.py`'s scenario manually — reconcile a sale to
`PAID`, then reconcile it again; the second response has `"applied": false` and the sale is
unchanged. The demo script (`evidence/product-pos/demo-sale-to-paid.sh`) does exactly this as its
last two steps before reading final state, specifically to prove replay safety against the real
deployed sandbox, not just in tests.

## 4. The background reconcile scheduler — `app/scheduler.py`

**What it is:** Payments has no push callback (see `services/_shared/pos-payments-contract.md`) —
POS is the one that polls. Without this scheduler, a sale only finds out it settled if something
explicitly calls `payment-reconcile` (e.g. a web frontend polling after showing an STK prompt).
`scheduler.py::run_once` sweeps every sale stuck in `PAYMENT_REQUESTED` past a staleness threshold
and reconciles each one, using the exact same `reconcile_sale` function the on-demand endpoint
uses — one code path, two triggers.

**Why it's off by default, and why that's deliberate, not an oversight:** `is_enabled()` gates the
whole thing on `POS_RECONCILE_SCHEDULER` — unset in pytest and in a fresh checkout, so it never
runs by accident. Turning it on needs no Platform/EventBridge wiring (unlike Commission's daily
disburse, which does) — it's a plain `asyncio` loop inside the same process (`reconcile_loop`),
which is the appropriate weight for "poll a handful of possibly-stuck sales every 30 seconds," not
a separate scheduled job.

**One bad sale can't take down the sweep:** `run_once` catches per-sale (`ReconcileAmountMismatch`
logged and skipped, any other exception logged and skipped) inside the loop over `stale_sales`, and
`reconcile_loop` itself also catches around the whole sweep — a bug reconciling one sale degrades
to "that one sale stays stuck a bit longer," never "the scheduler died and nothing gets reconciled
again until a restart."

**Demo it live:**
```bash
POS_RECONCILE_SCHEDULER=1 POS_RECONCILE_INTERVAL_SECONDS=5 POS_RECONCILE_STALE_AFTER_SECONDS=2 \
  python3 -m uvicorn app.main:app --port 8080 &
# create a sale, request payment, then just wait ~10s without calling payment-reconcile
# yourself — GET the sale and show it reached PAID on its own.
```
`tests/test_scheduler.py` covers the same thing deterministically (no real sleeping) by calling
`run_once()` directly against a sale whose `updated_at` is stale.

## 5. `GET /metrics` — `app/metrics.py` (ADR 0010, work item P-1)

**What it is:** a Prometheus text endpoint, using the `prometheus-client` library directly — unlike
Payments, POS has no zero-third-party-dependency constraint, so there was no reason to hand-roll
`Counter`/`Histogram` here. Five metrics: `pos_http_requests_total`,
`pos_http_request_duration_seconds` (both by route template), `pos_sale_creates_total{result}`,
`pos_payment_events_total{result}`, and `pos_sales_paid_total` (no labels).

**The rule that took the most care, same as Payments: no ids in labels, ever.** `route` is always
a template (`/tenants/{tenant_id}/sales/{sale_id}`), never the resolved path —
`test_route_label_is_a_template_not_a_resolved_path` proves two different tenants hitting "the
same" endpoint collapse into one label, not one per tenant. An unmatched path (something that
looks like it could be probing for a leaked id) buckets to `route="unmatched"` rather than ever
appearing verbatim in a label — `test_unmatched_path_is_bucketed_not_leaked` checks this with a
path containing a random UUID. `test_no_ids_ever_leak_into_metric_labels` then does the broad
version: create a sale, request payment, assert none of tenant/sale/till/attendant/product id
appear anywhere in `/metrics` output at all.

**Why `/health`, `/ready`, `/version`, `/metrics` are excluded entirely, not just labeled
differently:** ADR 0010 section 3 says "`/health` and `/ready` are excluded from SLI math" —
`EXCLUDED_ROUTES` in `app/metrics.py` is the simplest way to honor that literally: they never
generate a data point at all, so there's no risk of them accidentally being included in an error-
rate calculation later by someone who forgot to filter them out in a Grafana query.

**Why these five metrics and not more:** each one answers a specific question the ADR's SLIs need.
`pos_sale_creates_total{result="conflict"}` and `{result="invalid"}` are what catch a client
integration bug (a till app sending garbage) versus `{result="created"}`/`{result="replayed"}`
being normal traffic. `pos_payment_events_total{result="rejected"}` is the signal for "something
reconciled out of order" — should be rare; a sustained nonzero rate means either a real bug or a
sandbox with drifting clocks. `pos_sales_paid_total` is the top-line "is the product actually
working" number.

**Demo it live:**
```bash
cd services/pos && .venv/bin/pytest tests/test_metrics.py -v
curl -s localhost:8080/metrics | grep '^pos_'
```
Point at `pos_sale_creates_total{result="created"}` going up after a real sale, and at
`route="/tenants/{tenant_id}/sales/{sale_id}"` in the output rather than any real tenant/sale id.

## Suggested 6-minute narration

1. **(30s)** "POS owns the sale from creation to settlement — prices are always server-computed,
   and every payment outcome is treated as an event that might arrive twice." State the
   one-sentence version above.
2. **(90s)** Walk through server-side pricing and idempotent sale creation. Land on: the trust
   boundary (client never sends a price), and the payload-mismatch bug found while wiring up
   metrics.
3. **(90s)** Walk through the state machine / `apply_outcome` split. Land on: replay dedup by
   `event_id` vs. illegal-transition rejection, both recorded but only one applied — and why
   that's shared code (`reconcile_sale`) rather than duplicated per caller.
4. **(60s)** Run the reconcile scheduler demo, or show `test_scheduler.py` passing. Land on: off
   by default, one bad sale can't stop the sweep.
5. **(60s)** Walk through `/metrics` live. Land on: no ids in labels, route templating, and why
   each of the five metrics exists.
6. **(30s)** State what's *not* POS's: Grafana/alerts/EventBridge (Emebet, ADR 0010), and the web
   frontend (explicitly declared out of scope — ADR 0010's Product decision on question 1).

## Likely cross-system questions and honest answers

- **"Why does POS poll instead of Payments pushing a webhook?"** That's the contract as actually
  built (`services/_shared/pos-payments-contract.md`) — Payments has no outbound push in this
  design. Polling (on-demand plus the background sweep) is how POS compensates for that; it's a
  real, acknowledged gap, not an oversight — see the contract doc's own notes.
- **"What happens if two different terminals submit the same sale twice under load?"** Covered by
  the `IntegrityError` race path in `create_sale` — whichever request's `INSERT` loses the unique-
  constraint race re-queries and applies the same replay/conflict logic the first-checked request
  would have. Neither request gets an unhandled error; the loser gets the same answer the winner
  would have given it.
- **"Does the scheduler create load on Payments if nothing is actually stuck?"** No — it queries
  POS's own database first (`Sale.status == PAYMENT_REQUESTED` and `updated_at <= cutoff`) and only
  calls out to Payments for sales that match. A quiet system with nothing stuck makes zero calls.
- **"Why is the web frontend out of scope — didn't the brief ask for one?"** Product decision,
  not a missed deadline: the flow that's actually graded (sale → STK → paid, idempotency, replay
  safety) is fully proven end to end through the API directly, with real evidence against the
  live sandbox. Building a rushed frontend now would be new and far less verified than everything
  else shipped. See ADR 0010's Product decision section for the full reasoning.
