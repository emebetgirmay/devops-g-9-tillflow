# G3 Payments observability — walkthrough for Joy

This explains what got built in `services/payments` for ADR 0009 (Payments observability, G3),
why each piece looks the way it does, and how to demo and defend it live. Read this before your
individual defense — it's written to be narrated, not just referenced.

**Scope: this covers ADR 0009's work items G3-1, G3-2 and G3-3, plus G3-4 (k6).** Everything else
in G3 — Grafana, Slack alerts, the probe Lambda, EventBridge — is Emebet's (ADR 0010), not this
document. G3-5 (re-run k6 on Postgres) is blocked on RDS and isn't built; nothing to defend there,
just say so if asked.

## The one-sentence version

Payments now reports what it's doing three ways: numbers (`GET /metrics`), a story you can follow
(JSON logs carrying a `trace_id`), and a self-check (`GET /_admin/invariants`) that k6 uses to
prove none of the money-safety guarantees broke under load.

## 1. `GET /metrics` — `core/metrics.py`

**What it is:** a Prometheus text endpoint. Fourteen metrics, matching ADR 0009 section 2's
catalogue exactly (open the ADR side-by-side with `core/metrics.py` — the names line up 1:1).

**The one design decision worth explaining if asked "why does it look like this":** this service
has zero third-party dependencies, on purpose (there's a test — `test_no_outbound_guard.py` —
that fails the build if that changes, because the money path should never depend on a package
that could add an unreviewed network call). So there's no `prometheus_client` library here. The
`Counter`/`Histogram` classes in `core/metrics.py` are a small hand-rolled version that produces
the exact same text format. If asked "why not just use the standard library," that's the answer:
there isn't a metrics library *in* the standard library, and pulling in a third-party one would
break the guarantee the whole service is built around.

**The other thing worth being able to explain: four of the fourteen metrics aren't counters.**
`payments_records`, `payments_oldest_age_seconds`, `payments_reconcile_last_success_timestamp_seconds`,
and `payments_payouts_enabled` are computed **fresh from the database on every single scrape** —
look at `render_gauges()` in `core/metrics.py`, it's just SQL. Why: a counter that lives in
process memory resets to zero on every restart and is different on every task. These four numbers
need to be *the real current state*, the same no matter which task answers the scrape. That's
also why the doc says "aggregate with `max` in Grafana, never `sum`" — if two tasks both report
"there are 3 rows in NEEDS_REVIEW" (because they're reading the same database), summing them
would say 6, which is wrong.

**The rule that took the most care: no ids in labels, ever.** Every metric here uses `route`,
`operation`, `result`, `kind`, `state` — never a `payment_id`, `tenant_id` or phone number. Route
labels are *templates* (`"/payments/{id}"`), never the resolved path — look at `_ROUTE_PATTERNS`
in `app.py`. If you put a real id in a label, Prometheus creates a brand-new time series for every
single payment that's ever created, forever — that's a genuine outage risk (unbounded
cardinality), not just an style nitpick. `tests/test_metrics.py::NoIdsInLabelsTest` proves this
directly: it runs a real flow and asserts the tenant id, phone number, payment id and
disbursement id never appear anywhere in the `/metrics` output.

**Demo it live:**
```bash
cd services/payments && python3 app.py &
curl -s -X POST localhost:8080/payments -H 'Idempotency-Key: demo-key-0000000001' \
  -H 'content-type: application/json' \
  -d '{"tenant_id":"demo","msisdn":"254000000001","amount":50000,"account_reference":"demo"}'
curl -s -X POST localhost:8080/_fake/advance -d '{"seconds":5}'
curl -s -X POST localhost:8080/_fake/deliver-callbacks -d '{}'
curl -s localhost:8080/metrics | grep '^payments_'
```
Point at `payments_state_transitions_total{kind="payment",from_state="PENDING",to_state="SUCCEEDED"}`
and `payments_records{kind="payment",state="SUCCEEDED"}` — that second one is the database-derived
gauge; if you restart the process right now and curl `/metrics` again before creating anything
new, that number is still there (unlike the counters, which reset to zero).

## 2. Structured logs + trace propagation — `core/jsonlog.py`, `core/tracing.py`

**What it is:** one JSON line per HTTP request, one per state change (`event: "request"` /
`event: "state_transition"`), both carrying a `trace_id`. `core/store.py::transition()` — the
single place every state change already goes through — is where the state-change line is
emitted; `app.py::dispatch()` — the single place every request already goes through — is where
the request line is emitted and the trace id is set up.

**The design decision worth explaining: why `contextvars`, not just passing `trace_id` as a
parameter everywhere.** `Store.transition()` is called from deep inside `core/payments.py` and
`core/payouts.py`, many calls down from `app.py::dispatch()`. Threading a `trace_id` parameter
through every single function in that chain would touch dozens of signatures for one cross-cutting
concern. A `contextvars.ContextVar` (see `core/tracing.py::trace_context`) is Python's answer to
exactly this: it's set once, at the top, and anything running "underneath" that call — no matter
how many functions deep — can read it back without it being passed explicitly. The reason it's a
`ContextVar` and not a plain module-level variable: `ThreadingHTTPServer` handles each request on
its own thread, and a plain variable would let two concurrent requests overwrite each other's
trace id. A `ContextVar` is thread-local-safe by design.

**Why this matters for the actual product, not just as a nice-to-have:** Commission's daily
disburse run calls Payments' `/payouts` many times across possibly many attendants. Without a
shared trace id, if something goes wrong for one payout, you're grepping through logs for a
`disbursement_id` you may not even have handy yet. With it: `ledger/tracing.py` on Commission's
side generates **one** trace id per run (`disburse.py`'s `main()` calls `start_new_run()` once)
and sends it as a W3C `traceparent` header on every call to Payments; Payments picks that same
trace id back up and stamps every log line during that request with it. One `grep trace_id` finds
the whole run, on both sides.

**Demo it live:**
```bash
cd services/payments && python3 app.py 2>/tmp/payments.log &
curl -s -X POST localhost:8080/payments -H 'Idempotency-Key: demo-key-0000000002' \
  -H 'content-type: application/json' -H 'traceparent: 00-11111111111111111111111111111111-2222222222222222-01' \
  -d '{"tenant_id":"demo","msisdn":"254000000001","amount":50000,"account_reference":"demo"}' -D -
grep 11111111111111111111111111111111 /tmp/payments.log
```
Show that the `X-Trace-Id` response header and every log line for that request all carry the
exact trace id you sent in — that's the propagation working, not a generated one.

## 3. `GET /_admin/invariants` and k6 — `app.py::_invariants`, `k6/capacity.js`, `k6/correctness.js`

**What it is:** a fake-adapter-only endpoint (`404` under the real sandbox adapter — same gate as
`/_fake/*`) that runs four SQL checks k6 asserts stay true throughout a whole run:

1. `credits_equal_succeeded_payments` — every `SUCCEEDED` payment has exactly one ledger credit.
2. `duplicate_ledger_entries` — no provider reference ever got more than one ledger entry.
3. `payout_keys_with_multiple_live_disbursements` — no payout key ever has more than one
   non-`FAILED` disbursement.
4. `payments_declined_by_a_timeout` — a payment that timed out at initiation and one that was
   synchronously declined are structurally different code paths (`_finish_create`'s `if`/`elif`/
   `else` in `core/payments.py`); this checks that guarantee never broke.

**Why this is the real pass/fail signal, not the individual response codes:** load-testing a
system that's *supposed to* sometimes answer 409 (an idempotency conflict is a correct, intended
answer, not a bug) means you can't just check "did every request succeed." What actually matters
is "did the money-safety invariants hold for the whole run, no matter how many requests raced
each other." That's what `/_admin/invariants` checks, and it's why both k6 scripts call it in
`teardown()` — once, after everything else has finished — rather than per-request.

**`k6/capacity.js`** is the smoke/stepped/spike/soak envelope (a steady mix of payment and payout
creates, ramped up in stages) — this is what would produce "highest sustained RPS" for the SLO
doc, **except**: this build runs on SQLite, which serialises writers, so **the numbers here prove
correctness under concurrency, not real capacity** (ADR 0009 section 6 says this explicitly —
real sizing evidence needs Postgres, which is blocked on RDS). Don't claim a throughput number
from this as production sizing evidence if asked; say exactly that caveat.

**`k6/correctness.js`** deliberately drives the ugly cases: a replay storm (10 VUs hammering the
*same* `Idempotency-Key* for 8 seconds — only one create should ever happen), a duplicate submit
burst, a payment that uses the FakeAdapter's `254000000007` (`TIMEOUT_QUERY_RESOLVES`) magic
number so it times out at initiation and only resolves later via reconcile, and a payout using
`254000000102` (`INSUFFICIENT_FUNDS`) which should trip the payouts kill switch. It finishes by
checking `/metrics` directly for `payments_payouts_enabled 0` — proving the kill switch really
tripped, not just that the payout's own create call returned 201.

**Both scripts are verified, not just written** — I ran them against a real running instance of
this service before handing this off: `capacity.js` (shortened soak) passed with
`http_req_failed: 0.00%`, `checks: 100%`; `correctness.js` passed with `checks: 100%` across 5734
iterations, all five invariant checks green, and the kill-switch confirmation green. One real bug
I found and fixed while verifying: k6's default `http_req_failed` metric treats any non-2xx/3xx as
an error, which would have falsely flagged the *intended* 409 idempotency conflicts as failures —
fixed with `http.setResponseCallback(http.expectedStatuses(200, 201, 409))` at the top of both
scripts. If asked about this in your defense, it's a good example of "the load test needs to know
what your own system considers a legitimate outcome, not just what HTTP considers success."

**Run it yourself before your defense** (see `services/payments/README.md`'s "k6" section for the
exact commands and env vars) — you should be able to reproduce every number above.

## Suggested 6-minute narration

1. **(30s)** "Payments reports what it's doing three ways: metrics, traced logs, and a
   self-check k6 uses." State the one-sentence version above.
2. **(90s)** Walk through `/metrics` live (the demo commands above). Land on: no ids in labels
   (cardinality), and the four database-computed gauges (why they're not counters).
3. **(90s)** Walk through the trace propagation demo. Land on: `contextvars` solving "don't
   thread a parameter through 20 functions," and why Commission sending `traceparent` is what
   makes a whole disburse run followable.
4. **(90s)** Run `k6/correctness.js` live if time allows (it takes ~35s). Land on: invariants are
   the real pass/fail signal, and the capacity-vs-correctness distinction on SQLite.
5. **(30s)** State what's *not* yours: Grafana/alerts/probe/EventBridge (Emebet, ADR 0010), and
   G3-5 blocked on RDS.

## Likely cross-system questions and honest answers

- **"A Grafana panel shows `payments_records` at zero even though you just created a payment —
  why?"** Two real possibilities, in order of likelihood: the ADOT sidecar's Prometheus scrape
  isn't wired yet (that's Emebet's G3-7, not built as of this writing — check
  `infra/envs/sandbox/ecs.tf` for a Prometheus receiver pointed at this port), or Grafana's panel
  query is summing across tasks instead of taking `max` (see the aggregation note above).
- **"You said trace_id links Commission to Payments — show me it actually breaks if Commission
  doesn't send the header."** It doesn't break — `core/tracing.py::trace_id_from_traceparent`
  returns `None` for a missing/malformed header, and `trace_context` falls back to generating a
  fresh one. The log line still has *a* trace id, it just won't match anything on Commission's
  side. That's a deliberate design choice: a missing trace header should degrade tracing, not the
  request.
- **"Why didn't you add capacity numbers to the SLO doc?"** SQLite serialises writers, so any
  throughput number from k6 today is a correctness proof, not a sizing number — it would be
  actively misleading to write it into the SLO doc as if it were. That's blocked on Postgres/RDS,
  out of your control until Platform provisions it.
