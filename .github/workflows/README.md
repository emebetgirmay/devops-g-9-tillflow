# GitHub Actions — devops-g9

| Workflow | Trigger | Purpose |
|---|---|---|
| `pr.yml` | PRs to `main` | `terraform fmt/validate/plan` (OIDC) + POS docker build |
| `release.yml` | Push to `main` | Build/push POS by digest → ECS → smoke |

Requires repo Variables: `AWS_CI_ROLE_ARN`, `TF_STATE_BUCKET`, `ALB_DNS`.  
Environment: `sandbox` (for release job).
