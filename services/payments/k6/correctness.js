// Deliberate edge-case scenarios against the FakeAdapter (ADR 0009 section 6, ADR 0006's
// outline, work item G3-4): replay storm, duplicate submit, a timeout that later resolves via
// reconcile, and a payout that trips the kill switch. Ends by asserting GET /_admin/invariants
// never went bad — that's the actual pass/fail signal, not just individual response codes.
//
// Usage:
//   PAYMENTS_URL=http://127.0.0.1:8080 k6 run k6/correctness.js
//
// Run the server with FAKE_CLOCK=system (real elapsed time) and, for this script's short
// duration to actually exercise reconcile, with a short RECONCILE_SLA_SECONDS /
// CALLBACK_DEADLINE_SECONDS (e.g. 5) — production keeps its real defaults; this is a k6-harness
// override, same idea as ADR 0009 section 6's capacity caveat about SQLite vs Postgres.

import http from 'k6/http';
import { check, sleep } from 'k6';

// replay_storm and duplicate_submit deliberately expect an in-flight 409 sometimes (a request
// racing the very first one for the same Idempotency-Key) -- that's correct, not a failure. See
// k6/capacity.js's identical comment.
http.setResponseCallback(http.expectedStatuses(200, 201, 409));

const BASE = __ENV.PAYMENTS_URL || 'http://127.0.0.1:8080';

const JSON_HEADERS = { 'Content-Type': 'application/json' };
const REPLAY_KEY = 'k6-replay-storm-fixed-key';
const DUPLICATE_KEY = 'k6-duplicate-submit-fixed-key';

const REPLAY_BODY = JSON.stringify({
  tenant_id: 'k6-correctness',
  msisdn: '254000000001', // FakeAdapter: SUCCESS
  amount: 42000,
  account_reference: 'k6replay',
});
const DUPLICATE_BODY = JSON.stringify({
  tenant_id: 'k6-correctness',
  msisdn: '254000000001',
  amount: 43000,
  account_reference: 'k6dup',
});

function idKey(prefix) {
  return `${prefix}-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
}

export const options = {
  scenarios: {
    // All VUs hammer the exact same Idempotency-Key throughout: exactly one payment should
    // ever actually get created; every other response must be the identical replay (or, for a
    // request racing the very first one, the 409 in-flight answer) — never a second charge.
    replay_storm: {
      executor: 'constant-vus',
      vus: 10,
      duration: '8s',
      exec: 'replayStorm',
      startTime: '0s',
    },
    // Same idea, but as a fixed burst of concurrent identical submits rather than a sustained
    // storm — the classic "double-click the pay button" case.
    duplicate_submit: {
      executor: 'shared-iterations',
      vus: 20,
      iterations: 20,
      maxDuration: '10s',
      exec: 'duplicateSubmit',
      startTime: '10s',
    },
    // TIMEOUT_QUERY_RESOLVES (254000000007): initiate times out (UNKNOWN), but a later status
    // query resolves it -- proves "a timeout is not a decline" all the way through reconcile,
    // not just at creation.
    timeout_then_late_success: {
      executor: 'per-vu-iterations',
      vus: 5,
      iterations: 1,
      exec: 'timeoutThenLateSuccess',
      startTime: '22s',
    },
    // INSUFFICIENT_FUNDS (254000000102) trips the payouts kill switch (core/payouts.py's
    // TRIPPING_REASONS) -- proves a real documented failure actually pauses the run, not just
    // that one payout, and that the flag is readable afterwards.
    payout_kill_switch_trip: {
      executor: 'per-vu-iterations',
      vus: 1,
      iterations: 1,
      exec: 'payoutKillSwitchTrip',
      startTime: '24s',
    },
    // Keeps due callbacks/results flowing and runs the reconcile sweep repeatedly so the
    // timeout-then-late-success and kill-switch scenarios above actually get to resolve within
    // this script's short run.
    driver: {
      executor: 'constant-vus',
      vus: 1,
      duration: '35s',
      exec: 'driveFakeAdapter',
      startTime: '0s',
    },
  },
};

export function replayStorm() {
  const res = http.post(`${BASE}/payments`, REPLAY_BODY, {
    headers: { ...JSON_HEADERS, 'Idempotency-Key': REPLAY_KEY },
  });
  check(res, {
    'replay storm: 2xx or in-flight 409': (r) =>
      (r.status >= 200 && r.status < 300) || r.status === 409,
  });
}

export function duplicateSubmit() {
  const res = http.post(`${BASE}/payments`, DUPLICATE_BODY, {
    headers: { ...JSON_HEADERS, 'Idempotency-Key': DUPLICATE_KEY },
  });
  check(res, {
    'duplicate submit: 2xx or in-flight 409': (r) =>
      (r.status >= 200 && r.status < 300) || r.status === 409,
  });
}

export function timeoutThenLateSuccess() {
  const body = JSON.stringify({
    tenant_id: 'k6-correctness',
    msisdn: '254000000007', // FakeAdapter: TIMEOUT_QUERY_RESOLVES
    amount: 44000,
    account_reference: 'k6timeout',
  });
  const res = http.post(`${BASE}/payments`, body, {
    headers: { ...JSON_HEADERS, 'Idempotency-Key': idKey('corr-timeout') },
  });
  // The create call itself must still succeed (201, state PENDING/UNKNOWN) even though the
  // adapter's initiate call is about to time out underneath it -- that's the whole point.
  check(res, { 'timeout case: create is 201': (r) => r.status === 201 });
}

export function payoutKillSwitchTrip() {
  const body = JSON.stringify({
    tenant_id: 'k6-correctness',
    attendant_id: 'k6-kill-switch-attendant',
    payout_period: new Date().toISOString().slice(0, 10),
    msisdn: '254000000102', // FakeAdapter: INSUFFICIENT_FUNDS -> trips the kill switch
    amount: 60000,
  });
  const res = http.post(`${BASE}/payouts`, body, {
    headers: { ...JSON_HEADERS, 'Idempotency-Key': idKey('corr-kill') },
  });
  check(res, { 'kill switch case: create is 201': (r) => r.status === 201 });
}

export function driveFakeAdapter() {
  http.post(`${BASE}/_fake/deliver-callbacks`, '{}', { headers: JSON_HEADERS });
  http.post(`${BASE}/_admin/sweep`, '{}', { headers: JSON_HEADERS });
  sleep(1);
}

// Runs once, after every scenario above has finished -- the real pass/fail signal for this
// script, not the per-request checks (ADR 0009 section 6: "Add a fake-build GET
// /_admin/invariants returning the counts k6 asserts at the end").
export function teardown() {
  const res = http.get(`${BASE}/_admin/invariants`);
  const body = res.json();
  check(res, {
    'invariants: credits equal succeeded payments': () => body.credits_equal_succeeded_payments === true,
    'invariants: no provider ref with more than one ledger entry': () => body.duplicate_ledger_entries === 0,
    'invariants: no payout key with more than one live disbursement': () =>
      body.payout_keys_with_multiple_live_disbursements === 0,
    'invariants: no payment declined by a timeout': () => body.payments_declined_by_a_timeout === 0,
  });

  // Confirms payoutKillSwitchTrip() actually tripped the switch, not just that its create
  // call returned 201 -- the gauge is computed from the database (ADR 0009 section 1), so this
  // reads the real flag state, not an in-process assumption.
  const metricsRes = http.get(`${BASE}/metrics`);
  check(metricsRes, {
    'kill switch is tripped after the INSUFFICIENT_FUNDS payout': (r) =>
      r.body.includes('payments_payouts_enabled 0'),
  });
}
