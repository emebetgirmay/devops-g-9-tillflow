# G1 HOLD fixes (platform/g1-hold-fixes)

Addresses trainer HOLD (17 Sep 2026) P0-1…P0-4 and main P1 items.

| ID | Fix |
|---|---|
| P0-1 | API Gateway HTTP API + VPC Link + **internal** ALB |
| P0-2 | ADOT `/healthcheck`, OTLP receivers via SSM config, app `dependsOn` HEALTHY |
| P0-3 | Busybox placeholder image; service `ignore_changes = [task_definition]` |
| P0-4 | `release.yml` gated `terraform apply` of saved `plan.bin` on Environment `sandbox` |
| P1 | ALB probes `/ready` (gates on ADOT); mandatory API GW smoke + rollback; Trivy image scan; immutable tag reuse |

## Apply / verify

```bash
aws sso login --profile g9
export AWS_PROFILE=g9 AWS_REGION=eu-north-1
cd infra/envs/sandbox
terraform plan
terraform apply
terraform output api_gateway_url
curl -sS "$(terraform output -raw health_url)"
curl -sS "$(terraform output -raw ready_url)"
```

## GitHub Variables (update)

| Name | Value |
|---|---|
| `AWS_CI_ROLE_ARN` | existing |
| `TF_STATE_BUCKET` | existing |
| `API_GATEWAY_URL` | `terraform output -raw api_gateway_url` (no trailing slash) |

You can remove obsolete `ALB_DNS` after switching smoke to API GW.
