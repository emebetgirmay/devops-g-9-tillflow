# G4 drill: broken release caught and rolled back

**DRI:** Emebet Girmay (`@emebetgirmay`), Platform + delivery · **When:** 2026-09-29, 20:22–20:40 UTC
**Category:** broken release and rollback · **Type:** a real, unplanned failure, not a staged one
**Release run:** [#46](https://github.com/emebetgirmay/devops-g-9-tillflow/actions/runs/36625900401) ("Merge pull request #52")

All claims below are checked by [`collect.sh`](collect.sh) against GitHub's job record and CloudWatch:
[`checks.json`](checks.json), **9/9 pass**.

## What happened

PR #52 changed `services/_shared/`, which POS's deploy also watches, so the release rebuilt POS.
The rebuild resolved `fastapi>=0.128,<1.0` to **FastAPI 0.142.0**, released after POS's previous
build. That version starts OpenTelemetry by itself whenever `OTEL_*` variables are set (ECS sets
them for the ADOT sidecar) and exits when the OpenTelemetry extra is not installed:

```
ERROR:    Automatic OpenTelemetry export requires fastapi[opentelemetry] or fastapi[standard]. ...
ERROR:    Application startup failed. Exiting.
```

The image built, passed the Trivy scan (the bug is not a vulnerability) and was pushed. It could
not start.

## Timeline (UTC)

| Time | Event | Source |
|---|---|---|
| 20:22:54 | Build; Trivy image scan passes | `pipeline-steps.json` |
| 20:23:58 | New task definition **`devops-g9-pos:25`** registered (cloned from Terraform's `:23`); service updated. Revision **`:24`** keeps running | `pipeline-steps.json`, `pipeline-log.txt` |
| 20:24:46 | First `:25` task starts and exits: `Application startup failed` | `crash-log.txt` |
| 20:31:00 | ECS retries; the second `:25` task exits the same way | `crash-log.txt` |
| **20:34:15** | **Detected:** `aws ecs wait services-stable` gives up (`Max attempts exceeded`); the smoke step fails | `pipeline-log.txt` |
| 20:34:15 | **Automatic rollback** starts: `Smoke failed — rolling back to …devops-g9-pos:24` | `pipeline-log.txt` |
| 20:37:15 | A fresh `:24` task starts cleanly | `crash-log.txt` |
| **20:39:33** | **Rollback complete**: service stable on `:24`; `:25` shows `failedTasks: 1` | `pipeline-steps.json` |

- **Time to detect:** 10 min 15 s after the service update (the stabilisation waiter's limit).
- **Time to restore the intended state:** 5 min 18 s of rollback; 15 min 33 s from the deploy.

## User impact: none

| Signal, 20:22–20:42 | Result |
|---|---|
| Edge probe (public `/ready` through API Gateway), every minute | **100%** in all 20 minutes |
| POS healthy targets behind the ALB | **At least 1** in every minute: ECS kept `:24` serving while `:25` failed |
| POS 5xx / ALB-generated 5xx | **0 / 0** (20 POS requests in the window) |
| User-facing alarms | **None fired**, correctly: nothing a user could see broke |

Data: [`user-impact.json`](user-impact.json), [`alarm-changes.json`](alarm-changes.json).

## Why it was safe

1. **Rolling deployment:** ECS only drains the old revision once the new one is healthy, so a new
   revision that never becomes healthy never takes traffic.
2. **Mandatory smoke check** in `release.yml`: the release waits for the service to stabilise and
   calls `/health`, `/ready` and `/version` through the public edge before it counts as done.
3. **Automatic rollback** to the task definition that was running before the deploy, with its own
   stability wait.
4. **Immutable, digest-addressed images:** `:24` still pointed at the exact image that had been serving.

## What we changed (prevention)

| Change | PR |
|---|---|
| Pin `fastapi<0.142` for POS | #54 |
| CI starts the POS image **with ECS's `OTEL_*` environment** and requires it to come up, so this fails the pull request instead of the deploy. Verified: the check fails on the broken image and passes on the fixed one | #54 |
| POS redeployed with the pin: revision `:27`, smoke passed | #55's release |
| Recorded in the [scar log](../../../docs/scar-log.md) | |
| Follow-up for POS (`@Moraaalice`): exact pinned versions (a lock file), so a rebuild can never pick up a new release silently | open |

## Gaps this drill exposes

- **Detection takes about 10 minutes.** The pipeline waits for the full ECS stabilisation
  timeout. An ECS deployment circuit breaker, or a shorter wait with an early check for stopped
  tasks, would cut that to 1–2 minutes. Customer impact was nil because the old revision kept
  serving, but the release blocks the pipeline for that time.
- **POS failed on a dependency upgrade nobody made on purpose.** The CI start-up check now catches
  this class of failure; a lock file would prevent it.

## Reproduce the evidence

```bash
aws sso login --profile g9
./evidence/platform-delivery/g4-broken-release/collect.sh      # prints 9 PASS lines
```

To run it as a planned drill instead: release an image that exits at start-up (for example a
commit whose POS `CMD` exits non-zero), watch the smoke step fail and the rollback restore the
previous revision, then run `collect.sh` with that run's `RUN_ID`, `FROM` and `TO`.
