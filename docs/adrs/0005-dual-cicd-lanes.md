# ADR 0005 — Dual delivery lanes (GitHub Actions + CodePipeline)

- **Status:** Accepted (G1)
- **Date:** 2026-09-14
- **DRI:** Platform + delivery — Emebet Girmay (`@emebetgirmay`)

## Context

The brief requires two delivery lanes with one release authority: GitHub Actions for PR checks and gated Terraform, and AWS CodePipeline for build → scan → ECR → ECS → smoke/rollback. Path filters should limit rebuilds to changed services.

## Decision

| Lane | Role in devops-g9 |
|---|---|
| **GitHub Actions** | PR: lint/tests/scans + `terraform plan`. `main`: OIDC-authenticated `terraform apply` via role `devops-g9-ci-deploy` and a protected environment. |
| **AWS CodePipeline** | Source (CodeConnections) → CodeBuild (test/build/SBOM/scan) → ECR (immutable SHA/digest) → ECS deploy → post-deploy smoke; rollback on failure. |
| **Release authority** | Only digests that passed the scan gate are deployable. No `latest` tags. |

### G1 scope vs later

- **G1:** GHA `plan` on PR + OIDC apply path; one service (`pos` stub) deployable by digest; CodePipeline **skeleton** or deferred with this ADR documenting intent.
- **G2+:** Per-service path filters/matrix; full CodePipeline promotion + smoke/rollback evidence.

## Consequences

- Platform owns pipeline IAM, OIDC trust (repo-scoped), and failure policy.
- Product/Payments own service tests that the pipelines run.
- Evidence for G1 is plan/apply parity + first ECS deploy URL, not full dual-lane polish.

## Alternatives considered

| Option | Rejected because |
|---|---|
| GHA only | Brief requires CodePipeline as a defended lane |
| CodePipeline only | Loses PR-native plan comments and GitHub environment gates |
| Deploy on every push without scans | Violates golden-path security gates |
