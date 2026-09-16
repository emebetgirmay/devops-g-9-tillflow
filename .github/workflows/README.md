# GitHub Actions — devops-g9

| Workflow | Trigger | Purpose |
|---|---|---|
| `pr.yml` | PRs to `main` | gitleaks, Trivy IaC, terraform fmt/validate/plan (OIDC), POS docker build |
| `release.yml` | Push to `main` | Build/push POS by digest → ECS → smoke |

Requires repo Variables: `AWS_CI_ROLE_ARN`, `TF_STATE_BUCKET`, `ALB_DNS`.  
Environment: `sandbox` (for release job).

Local SHA deploy (before CI): `./scripts/build-push-pos.sh && ./scripts/deploy-pos-sha.sh`
