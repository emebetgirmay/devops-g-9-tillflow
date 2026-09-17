# G0 close-out + G1 Platform — runbook (Group 9)

**Region:** `eu-north-1` · **Account:** `240462142849` · **Branch:** `platform/g1-foundation`  
**You:** Emebet (`@emebetgirmay`) — Platform + Reliability

Use one terminal. Do steps in order.

---

## 0) AWS login (every new session)

```bash
aws sso login --profile g9
export AWS_PROFILE=g9 AWS_REGION=eu-north-1
aws sts get-caller-identity
# expect Account 240462142849
```

```bash
cd ~/devops-g-9-tillflow
git checkout platform/g1-foundation
```

---

## 1) Close G0 follow-ups (docs — today)

Already in this branch:

- [x] Final ownership (no swap note)
- [x] Default reviewer = Joy on infra
- [x] Your sign-off dated
- [x] ADR 0005 dual CI/CD
- [x] Stub ADR 0006 (Joy) + 0007 (Alice)

**You still do:**

1. Confirm mentor is a repo collaborator  
2. Slack Alice + Joy (copy/paste below)  
3. When they reply “signed …”, fill dates in `docs/ownership.md`

```text
Alice + Joy — G0 passed; closing follow-ups while I finish G1.

1) Reply: signed 2026-09-16
2) Joy — fill docs/adrs/0006-idempotency-replay.md (your PR)
3) Alice — fill docs/adrs/0007-multi-tenancy.md + any Product PR (you need commits)
4) Joy — review my upcoming infra PR (you are default reviewer)
```

Alice/Joy ADRs can land **after** your G1 PR — don’t block apply on them.

---

## 2) Terraform: ECS golden path

Bootstrap + VPC should already exist. Apply the new resources (ECR/ALB/ECS/OIDC):

```bash
cd ~/devops-g-9-tillflow/infra/envs/sandbox
terraform init -backend-config=backend.hcl
terraform plan
terraform apply
terraform output
# note: health_url, ci_role_arn, ecr_pos_url
```

---

## 3) Push first image + stabilize ECS

```bash
cd ~/devops-g-9-tillflow
chmod +x scripts/build-push-pos.sh evidence/platform-delivery/collect.sh
./scripts/build-push-pos.sh bootstrap

aws ecs update-service \
  --cluster devops-g9 \
  --service devops-g9-pos \
  --force-new-deployment

aws ecs wait services-stable \
  --cluster devops-g9 \
  --services devops-g9-pos

cd infra/envs/sandbox
curl -sS "$(terraform output -raw health_url)"
curl -sS "$(terraform output -raw ready_url)"
```

Expect JSON `status: ok/ready`. If curl fails, wait 1–2 minutes and retry; check:

```bash
aws ecs describe-services --cluster devops-g9 --services devops-g9-pos \
  --query 'services[0].{running:runningCount,events:events[0].message}'
```

---

## 4) Collect evidence (required for trainer)

```bash
cd ~/devops-g-9-tillflow
./evidence/platform-delivery/collect.sh
ls evidence/platform-delivery/
# expect: outputs.json smoke-*.json ecs-*.json/txt tag-audit.*
```

Confirm `ecs-containers.txt` shows **pos** and **adot-collector** (or `adot`) **RUNNING**.

---

## 5) Wire GitHub Actions (OIDC)

1. Repo → **Settings → Secrets and variables → Actions → Variables**
2. Add:
   - `AWS_CI_ROLE_ARN` = `terraform output -raw ci_role_arn`
   - `TF_STATE_BUCKET` = `devops-g9-tfstate-240462142849`
   - `ALB_DNS` = ALB DNS from `terraform output -raw alb_dns_name` (no `http://`)
3. Repo → **Settings → Environments** → create `sandbox` (optional protection rules)

Workflows already added: `.github/workflows/pr.yml`, `release.yml`.

---

## 6) Commit, push, PR

```bash
cd ~/devops-g-9-tillflow
git add -A
git status
git commit -m "$(cat <<'EOF'
feat(platform): G1 foundation — bootstrap, VPC, ECS golden path, OIDC CI

Close G0 follow-ups (ownership cleanup, ADR 0005) and land sandbox
platform with POS health stub, ADOT sidecar, evidence collector, and GHA.
EOF
)"
git push -u origin platform/g1-foundation
gh pr create --title "G1 Platform — ECS golden path + OIDC CI" --body "$(cat <<'EOF'
## Summary
- G0 follow-ups: ownership finalized, ADR 0005, stubs for 0006/0007
- Bootstrap + sandbox VPC (eu-north-1)
- ECR/ALB/ECS `devops-g9-pos` with app + ADOT sidecar
- OIDC role `devops-g9-ci-deploy` + PR/Release workflows
- Evidence collector under `evidence/platform-delivery/`

## Test plan
- [ ] `curl` /health and /ready return 200
- [ ] ECS task has 2 containers RUNNING
- [ ] `./evidence/platform-delivery/collect.sh` committed artifacts
- [ ] Joy reviews (CODEOWNERS)
- [ ] GitHub variables AWS_CI_ROLE_ARN / TF_STATE_BUCKET set
EOF
)"
```

Ask Joy to approve.

---

## 7) Slack trainer (after PR + evidence)

```text
Hey — Group 9 G1 ready for review.
PR: <url>
Region eu-north-1. Evidence in evidence/platform-delivery/ (smoke + ECS 2 containers + tags).
G0 follow-ups included (ownership cleanup + ADR 0005; 0006/0007 stubbed for Joy/Alice).
```

---

## Done when

- [ ] `/health` + `/ready` work  
- [ ] 2/2 containers on ECS  
- [ ] Evidence files committed  
- [ ] PR open + Joy review  
- [ ] OIDC vars set (plan job can run)  
- [ ] G0 sign-offs filled when teammates reply  
- [ ] Mentor has access  

**Not required to block G1:** full CodePipeline, RDS/Redis, Daraja, Grafana (later gates).
