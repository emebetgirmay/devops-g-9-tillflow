# Threat model — TillFlow (G0 draft)

**Method:** lightweight STRIDE on trust boundaries · **Owner:** Product + Payments DRIs co-author; Platform implements controls · **Sandbox only:** Daraja 3.0; no real money or customer PII in git.

## Assets

| Asset | Why it matters |
|---|---|
| Sale / payment / payout ledger | Double-charge or double-pay = product failure |
| Daraja credentials & access tokens | Account takeover of till / B2C |
| Tenant & attendant identity / roles | Cross-tenant data leak |
| Terraform state & pipeline OIDC roles | Infra compromise |
| Slack webhook | Alert spam / phishing via ops channel |

## Trust boundaries

1. Internet → API Gateway / web
2. Gateway → VPC Link → private ALB → ECS
3. Payments → Daraja sandbox HTTPS
4. CI (GitHub OIDC / CodeBuild) → AWS deploy roles
5. Humans → Secrets Manager / GitHub Environments

## STRIDE summary

| Threat | Example | Mitigation (G0 intent) |
|---|---|---|
| **S**poofing | Forged STK callback | Validate Daraja callback authenticity; idempotent state machine; never trust body alone for money transitions |
| **T**ampering | Replay callback / duplicate B2C | Idempotency keys; single legal transition; payout ledger unique constraint; DLQ for poison messages |
| **R**epudiation | “We never got paid” | Immutable-ish ledger rows; structured JSON logs with `trace_id`; S3 evidence |
| **I**nformation disclosure | Cross-tenant reads; secrets in logs | Tenant-scoped authZ; per-service DB roles; no secrets in git/TF state outputs/build logs; block-public S3 |
| **D**enial of service | Callback flood; cache outage | Rate limits at edge; SQS buffering; degrade gracefully; alerts |
| **E**levation of privilege | Commission calling Daraja with broad creds | Commission → Payments API only; least-privilege IAM task roles; separate secrets |

## Explicit non-goals (sandbox)

- PCI card data (out of scope — M-Pesa only)
- Production Safaricom credentials
- Real phone numbers / KYC data in fixtures

## Abuse cases to prove later (G4)

1. Timeout then reconcile — no second charge
2. Reordered/duplicate callbacks — one ledger effect
3. Broken release — smoke fail → ECS rollback
4. Restore + reconcile provider refs before “recovered”

## Open risks

| Risk | Owner | Status |
|---|---|---|
| Callback signature verification details in sandbox | Payments | TBD by G2 |
| OIDC trust conditions too broad | Platform | TBD by G1 |
| Slack webhook exfiltration | Reliability | Secrets Manager only |
