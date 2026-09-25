# evidence/platform-delivery — G1 reproduction

DRI: Emebet Girmay (`@emebetgirmay`)

## What G2 adds

- Payments ECS service on the same internal ALB, `PAYMENTS_BASE_URL` on POS
- POS can open port 80 to that ALB (VPC CIDR ingress on the ALB, port 80 egress on the POS task)
- `smoke-payments-sweep.json` is `POST /_admin/sweep` through API Gateway (Payments path rule)
- `sg-pos-payments.txt` shows the port 80 rules that let the POS task reach Payments

## What G1 proves

- Terraform in `eu-north-1` under prefix `devops-g9`
- Bootstrap state bucket + lock table
- VPC 2 AZ + public ALB + ECS Fargate `devops-g9-pos`
- Task runs **pos** + **adot-collector**
- Public `/health`, `/ready`, `/version`
- GitHub OIDC role `devops-g9-ci-deploy` trusts the **existing** account OIDC provider (data source; cohort roles cannot create the provider)
- Evidence regenerated via `./collect.sh`

## Collect

```bash
export AWS_PROFILE=g9 AWS_REGION=eu-north-1
./evidence/platform-delivery/collect.sh
```

## Expected files after collect

| File | Shows |
|---|---|
| `outputs.json` | ALB / health URLs / CI role |
| `smoke-health.json` | `/health` |
| `smoke-ready.json` | `/ready` |
| `smoke-version.json` | `/version` |
| `ecs-tasks.json` / `ecs-task-detail.json` / `ecs-containers.txt` | POS containers RUNNING |
| `ecs-payments-tasks.json` / `ecs-payments-task-detail.json` / `ecs-payments-containers.txt` | Payments containers RUNNING |
| `smoke-payments-sweep.json` | Payments `/_admin/sweep` via API Gateway |
| `sg-pos-payments.json` / `sg-pos-payments.txt` | ALB ingress and POS egress on port 80 |
| `tag-audit.json` / `tag-audit.txt` | required tags |

## Manual apply order

See [`docs/g0-g1-runbook.md`](../../docs/g0-g1-runbook.md).
