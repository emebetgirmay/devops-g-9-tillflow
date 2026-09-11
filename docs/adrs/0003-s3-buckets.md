# ADR 0003 — S3 bucket layout

- **Status:** Proposed (G0 — decide before G1)
- **Date:** 2026-09-09
- **DRI:** Platform + delivery — Emebet Girmay (`@emebetgirmay`)
- **Proof:** ADR + bucket policies

## Context

Need separate purposes for state, artifacts, logs, backups, and evidence. Bucket names are globally unique.

## Decision

One bucket **per purpose**, all prefixed `devops-g9-` and suffixed with account id (or short suffix):

| Purpose | Example name | Controls |
|---|---|---|
| Terraform state | `devops-g9-tfstate-<account>` | Versioning, KMS, BPA, DynamoDB lock `devops-g9-tflock` |
| Pipeline artifacts | `devops-g9-artifacts-<account>` | Versioning, KMS, BPA, lifecycle |
| ALB / access logs | `devops-g9-logs-<account>` | BPA, lifecycle retention |
| Backups / exports | `devops-g9-backups-<account>` | Versioning, KMS, BPA, retention ↔ RPO |
| Evidence pack | `devops-g9-evidence-<account>` | Versioning, KMS, BPA |

Common controls for **every** bucket: versioning ON, SSE-KMS, block public access **all four**, least-privilege bucket policy, tags per ownership.md.

## Consequences

- State backend configured before first apply of app stacks
- No public ACLs; evidence uploads via authenticated roles only

## Alternatives

- Single mega-bucket with prefixes — harder lifecycle/IAM story; rejected
