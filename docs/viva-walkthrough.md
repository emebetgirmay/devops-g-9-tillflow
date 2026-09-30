# Viva walkthrough — TillFlow, Group 9

A live run of about 20 minutes, then questions. Each block names who drives it. Have open before
starting: this page, a terminal with `aws sso login --profile g9` done, Grafana
(https://honestmesa567.grafana.net, dashboard **TillFlow overview**), Slack `#devops-g9-alerts`,
and the repository on GitHub.

Public URL: **https://nilrqzkq8a.execute-api.eu-north-1.amazonaws.com** (changed with the G5 rebuild).

## 1. What it is (2 min) · Emebet

- TillFlow: shop tills take a sale, the customer pays by M-Pesa STK push, and each night attendants
  are paid their commission by B2C. Three services: **POS** (`@Moraaalice`), **Payments** and
  **Commission** (`@chesangJ`); platform and reliability (`@emebetgirmay`).
- Path: internet → API Gateway → VPC link → internal ALB → ECS Fargate (POS, Payments; Commission
  as scheduled tasks) → RDS PostgreSQL. Picture: [`architecture.md`](architecture.md).
- One sentence on money safety: *a timeout is never a decline, a callback is never applied twice,
  and nobody re-sends a payment by hand.*

## 2. A sale, live (3 min) · Alice

```bash
AWS_PROFILE=g9 ./evidence/product-pos/demo-sale-to-paid.sh
```

Show: the sale created with an Idempotency-Key, the STK push through Payments, the fake provider's
callback, the sale `PAID`. Then replay the same request: same sale, no second charge.

## 3. One trace from sale to callback (2 min) · Joy

```bash
./evidence/reliability-ops/xray/collect-trace.sh
```

Open the console link it prints: POS's request, Payments' `POST /payments` under it, and the
provider's callback settling the payment **inside the sale's trace**, although it arrives as its own
request (Payments stores the creating span with the payment).

## 4. Operate: dashboards, SLOs, alerts (3 min) · Emebet

- Grafana **TillFlow overview**: availability 5 m / 1 h / 28 d against the SLO, budget left, burn
  rate with the 14.4x and 6x lines; POS and Payments dashboards for RED and saturation.
- Every alarm posts to Slack when it fires **and** when it recovers, with the runbook link, owner and
  first safe action. Show the G3 drill posts and the G4 database reboot posts.
- k6 (FakeAdapter only): **36.7 req/s sustained, p95 154 ms, 0.00% failed**. The load test found a
  thread-safety bug, a monitoring blind spot and CPU saturation, all fixed
  ([`k6-analysis.md`](../evidence/reliability-ops/k6-analysis.md)).

## 5. Recover: the drills (4 min) · Emebet, Joy, Alice

| Drill | Number to say |
|---|---|
| Restore RDS to a point in time | **RPO 2 min 36 s, RTO 18 min 44 s**; found a runbook command that could not run |
| Payments task killed | back in **76 s**, nothing lost; found an alarm blind to total outages, fixed |
| Database rebooted | both services recovered alone; Payments slower (finding 6, fix with Joy) |
| Broken release | crashed at start-up, **rolled back automatically**, no user impact |
| Uncertain payment and payout, callback replay (Joy) | a timeout stays `UNKNOWN` and is reconciled; replays change nothing |
| Sale stuck on the no-push gap (Alice) | unsafe retry refused (409); reconcile recovers it, no second push |

Evidence: [`g4-g5-evidence.md`](../evidence/g4-g5-evidence.md).

## 6. Release and defences (3 min) · Emebet

- A PR runs gitleaks, Trivy (config and images), tests on SQLite **and PostgreSQL**, and a
  Terraform plan. `main` builds once, pushes by digest, writes an SBOM, applies **the reviewed plan**
  behind an approval, deploys, smoke-tests and **rolls back** on failure.
- No long-lived keys: GitHub OIDC into a role scoped to our names and tags. Secrets only in Secrets
  Manager; CI can create them but never read them.
- **Destroy and rebuild:** everything destroyed and rebuilt from `main`, green in **43 min** (rebuild
  20 min), nothing billable left behind.
- Cost: **about $151 a month**; the biggest lines are metrics and the NAT gateway. A forgotten lab
  stack in the same region cost more than TillFlow; we found it from the bill.

## 7. What is not done, said first (1 min) · Emebet

- Callbacks are handled synchronously, with the scheduled reconcile as the safety net; SQS with a
  DLQ is the agreed next step (ADR 0009 question 5).
- Commission's schedules switch on once `disburse.py --check` lands and a sandbox tenant exists.
- Single-AZ database: RTO is 18 minutes; Multi-AZ would cut it to a failover of a minute or two for
  about $12 a month more.
- No web UI; the POS API is the product surface.

## Questions to expect

| Question | Short answer | Who |
|---|---|---|
| Why is a timeout not a decline? | The customer may have paid; `UNKNOWN` is reconciled against the provider, never guessed | Joy |
| How do you know a callback was not applied twice? | One transition per state, compare-and-swap on the current state, a unique provider reference; the ledger invariant (credits = succeeded payments) is checked | Joy |
| What happens if Payments is down during a sale? | POS keeps the sale unpaid, refuses an unsafe retry, and reconciles when Payments is back | Alice |
| Why one database with schemas, not three? | Cost and operations for a sandbox; isolation by role and schema, tested (a role cannot read another schema) | Emebet |
| What pages someone at night? | Fast burn (14.4x over 5 min), service down 2 min, probe down, critical money anomaly | Emebet |
| How would you scale Payments? | Now that state is in RDS, more tasks behind the ALB; CPU alarm at 70% is the signal | Emebet |
| What would you do next? | SQS for callbacks, Multi-AZ for production, a custom domain so the URL survives a rebuild | All |
