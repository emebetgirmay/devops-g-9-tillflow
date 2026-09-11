# ADR 0001 — AWS Region

- **Status:** Accepted (G0)
- **Date:** 2026-09-11
- **DRI:** Platform + delivery — Emebet Girmay (`@emebetgirmay`)

## Context

TillFlow must deploy in **exactly one assigned AWS Region**. All nameable resources use prefix `devops-g9`. Latency to Daraja sandbox, ECS/RDS availability, and mentor account constraints matter.

## Decision

**Deploy exclusively in `eu-north-1` (Stockholm)** — assigned group region.

Rationale:

- Mentor/group assignment is `eu-north-1`
- Full support for ECS Fargate, RDS, ElastiCache, API Gateway VPC Link
- Single-region keeps cost and blast radius small for a 14-day capstone

## Consequences

- All Terraform `provider "aws" { region = "eu-north-1" }` and resource ARNs use this region
- S3 bucket names remain globally unique: e.g. `devops-g9-tfstate-<account-id>`
- Multi-AZ means **two AZs inside `eu-north-1`**, not multi-region
- No resources in other regions earn evidence credit

## Alternatives considered

| Option | Rejected because |
|---|---|
| `eu-west-1` | Not the assigned region |
| `us-east-1` | Not assigned; higher latency for EAT teammates |
| Multi-region active-active | Out of scope / cost for capstone |
