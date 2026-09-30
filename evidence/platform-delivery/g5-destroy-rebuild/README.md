# G5: destroy and rebuild, timed

**Run:** 2026-09-30 by Emebet Girmay (`@emebetgirmay`), from a laptop with an SSO session, with
[`infra/scripts/destroy-rebuild.sh`](../../../infra/scripts/destroy-rebuild.sh) (runbook
["Destroy and rebuild"](../../../docs/runbook.md#destroy-and-rebuild)). Everything in
`infra/envs/sandbox` was destroyed and built again from `main` (`e4a66c6`); only the Terraform state
bucket and lock table survive by design.

## Result: green 43 minutes after the destroy started

| Phase | UTC | Minutes |
|---|---|---|
| `terraform destroy` (RDS final snapshot included) | 19:12:58 → 19:36:08 | **23.2** |
| `terraform apply` from nothing | 19:36:10 → 19:45:32 | **9.4** |
| Database roles and schemas (`rds-bootstrap.sh`) | → 19:46:41 | 1.1 |
| Slack webhook set again (typed, hidden) | 19:48:08 | |
| Release on the new stack (`workflow_dispatch`): build, Trivy, SBOM, deploy POS, Payments, Commission, each with its smoke check | 19:49:58 → 19:55:34 | **5.6** |
| Smoke through the new public URL | 19:56:24 | |
| **Rebuild, apply → green** | | **20.2** |
| **Destroy start → green** | | **43.4** |

[`timeline.json`](timeline.json), [`run.txt`](run.txt) (the console; no secret in it).

**Smoke, 6/6** ([`smoke.json`](smoke.json)), through `https://nilrqzkq8a.execute-api.eu-north-1.amazonaws.com`:
health, ready, a POS write, a Payments write, that payment settling through the fake provider's
callback, and Payments' invariants.

## What was left behind, and why ([`leftovers.json`](leftovers.json))

| Still tagged `group=devops-g9` after the destroy | Why |
|---|---|
| State bucket, lock table | Kept by design (`infra/bootstrap`) |
| `devops-g9-db-final` DB snapshot | Kept by design: the data as it was at the destroy |
| Alerts KMS key | Pending deletion (7-day minimum) |
| NAT gateway | Already `deleted`; the tagging API lags |
| `devops-g9/daraja` secret | Created by hand, deliberately outside Terraform (ADR 0004) |
| 23 ECS task definition revisions | `INACTIVE` history AWS keeps; not billed |

Nothing that runs or bills was left behind.

## What the run found

1. **The script stopped at `gh variable set API_GATEWAY_URL` (HTTP 403).** Two GitHub accounts were
   signed in and the active one had read access only. The rebuilt stack was complete; the variable,
   the release and the smoke were finished by hand at the times above. **Fixed:** the script now
   says what to set and waits instead of stopping.
2. **The public URL changes on every rebuild** (a new API Gateway id): the release smoke reads it
   from the Actions variable, which the script now sets before releasing. README and the evidence
   scripts carry the new URL; a custom domain would remove this step.
3. **The rebuilt database starts empty** (sandbox, FakeAdapter). The data is recoverable from
   `devops-g9-db-final`; restoring RDS data is proven separately by the
   [G4 restore drill](../../reliability-ops/g4-game-day.md) (RPO 2 min 36 s, RTO 18 min 44 s).
