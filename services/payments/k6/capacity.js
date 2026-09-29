// Capacity envelope against the FakeAdapter only (ADR 0009 section 6, work item G3-4).
//
// Run the server with FAKE_CLOCK=system (real elapsed time, so timeouts genuinely elapse
// instead of needing manual /_fake/advance calls) and against SQLite, per ADR 0009 section 6's
// own caveat: "SQLite serialises writers, so the numbers prove correctness under concurrency,
// not capacity." Real sizing evidence needs the Postgres backend (blocked on RDS, ADR 0002).
//
// Usage:
//   PAYMENTS_URL=http://127.0.0.1:8080 k6 run k6/capacity.js
//   SOAK_DURATION=15m k6 run k6/capacity.js   # the real evidence run (default below is short,
//                                              for iterating on the script itself)
//
// Thresholds below mirror docs/slo-error-budgets.md.

import http from 'k6/http';
import { check, sleep } from 'k6';

// A payout intentionally answers 409 for a legitimate idempotency collision (same
// tenant/attendant/period already requested) -- that's correct behaviour, not a failure, so it
// must not count against the "failed requests < 1%" threshold below. Without this,
// http_req_failed treats any non-2xx/3xx as an error regardless of what the checks think.
http.setResponseCallback(http.expectedStatuses(200, 201, 409));

const BASE = __ENV.PAYMENTS_URL || 'http://127.0.0.1:8080';
const SOAK_DURATION = __ENV.SOAK_DURATION || '20s';
const DRIVER_DURATION = __ENV.DRIVER_DURATION || '90s';

function idKey(prefix) {
  return `${prefix}-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
}

export const options = {
  thresholds: {
    http_req_failed: ['rate<0.01'],
    http_req_duration: ['p(95)<500'],
    checks: ['rate>0.99'],
  },
  scenarios: {
    // Keeps the FakeAdapter's due callbacks/results flowing throughout the whole run — the
    // harness ADR 0009 section 6 asks for, since this build has no push callback of its own
    // under load (production gets real Daraja callbacks instead).
    driver: {
      executor: 'constant-vus',
      vus: 1,
      duration: DRIVER_DURATION,
      exec: 'driveFakeAdapter',
      startTime: '0s',
    },
    smoke: {
      executor: 'constant-vus',
      vus: 1,
      duration: '10s',
      exec: 'steadyMix',
      startTime: '0s',
    },
    stepped: {
      executor: 'ramping-vus',
      startVUs: 0,
      stages: [
        { target: 5, duration: '10s' },
        { target: 10, duration: '10s' },
        { target: 20, duration: '10s' },
      ],
      exec: 'steadyMix',
      startTime: '10s',
    },
    spike: {
      executor: 'ramping-vus',
      startVUs: 0,
      stages: [
        { target: 50, duration: '5s' },
        { target: 50, duration: '10s' },
        { target: 0, duration: '5s' },
      ],
      exec: 'steadyMix',
      startTime: '40s',
    },
    soak: {
      executor: 'constant-vus',
      vus: 10,
      duration: SOAK_DURATION,
      exec: 'steadyMix',
      startTime: '60s',
    },
  },
};

export function steadyMix() {
  if (Math.random() < 0.2) {
    createPayout();
  } else {
    createPayment();
  }
  sleep(Math.random() * 0.5);
}

function createPayment() {
  const body = JSON.stringify({
    tenant_id: 'k6-capacity',
    msisdn: '254000000001', // FakeAdapter: SUCCESS
    amount: 10000 + Math.floor(Math.random() * 90000),
    account_reference: 'k6load',
  });
  const res = http.post(`${BASE}/payments`, body, {
    headers: { 'Content-Type': 'application/json', 'Idempotency-Key': idKey('cap-pay') },
  });
  check(res, { 'payment create is 2xx': (r) => r.status >= 200 && r.status < 300 });
}

function createPayout() {
  // A handful of distinct attendants: enough to see real creates, few enough that repeats
  // legitimately collide on (tenant, attendant, period) sometimes — a 409 there is Commission's
  // own idempotency contract working, not a load-test failure, so the check accepts it too.
  const body = JSON.stringify({
    tenant_id: 'k6-capacity',
    attendant_id: `k6-attendant-${Math.floor(Math.random() * 20)}`,
    payout_period: new Date().toISOString().slice(0, 10),
    msisdn: '254000000101', // FakeAdapter: SUCCESS
    amount: 50000,
  });
  const res = http.post(`${BASE}/payouts`, body, {
    headers: { 'Content-Type': 'application/json', 'Idempotency-Key': idKey('cap-out') },
  });
  check(res, {
    'payout create is 2xx or an idempotent conflict': (r) =>
      (r.status >= 200 && r.status < 300) || r.status === 409,
  });
}

export function driveFakeAdapter() {
  http.post(`${BASE}/_fake/deliver-callbacks`, '{}', {
    headers: { 'Content-Type': 'application/json' },
  });
  http.post(`${BASE}/_admin/sweep`, '{}', { headers: { 'Content-Type': 'application/json' } });
  sleep(1);
}

// Bottleneck/headroom/cost-assumption analysis goes in the evidence writeup alongside this
// run's JSON summary (`k6 run --summary-export=...`), per ADR 0009 section 6 and the capstone's
// "Measure and visualize" section — not computed here.
