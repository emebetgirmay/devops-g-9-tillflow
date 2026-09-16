# G0 close-out + G1 finish — step-by-step (do in order)

**Branch:** `platform/g1-foundation` · **Region:** `eu-north-1` · **Profile:** `g9`

---

## STEP 1 — Login (every new terminal)

```bash
aws sso login --profile g9
export AWS_PROFILE=g9 AWS_REGION=eu-north-1
aws sts get-caller-identity
cd ~/devops-g-9-tillflow
git checkout platform/g1-foundation
```

✅ Expect account `240462142849`.

---

## STEP 2 — GitHub Variables + Environment (browser)

Open: https://github.com/emebetgirmay/devops-g-9-tillflow/settings/variables/actions

Add variables:

| Name | Value |
|---|---|
| `AWS_CI_ROLE_ARN` | `arn:aws:iam::240462142849:role/devops-g9-ci-deploy` |
| `TF_STATE_BUCKET` | `devops-g9-tfstate-240462142849` |
| `ALB_DNS` | `devops-g9-alb-2139590754.eu-north-1.elb.amazonaws.com` |

Then: **Settings → Environments → New** → name `sandbox` → Create.

✅ No push required for this step.

---

## STEP 3 — Deploy POS by git SHA (matches G10 evidence)

```bash
cd ~/devops-g-9-tillflow
export AWS_PROFILE=g9 AWS_REGION=eu-north-1
chmod +x scripts/*.sh evidence/platform-delivery/collect.sh

# uses current commit SHA as immutable tag + deploys by digest
./scripts/build-push-pos.sh
./scripts/deploy-pos-sha.sh
```

`deploy-pos-sha.sh` waits for ECS then curls `/health`, `/ready`, `/version`.

✅ `/version` should show your commit SHA (not `local`).  
✅ If wait is slow, leave it; press `q` only if stuck in a pager (`:` prompt).

---

## STEP 4 — Collect evidence again

```bash
./evidence/platform-delivery/collect.sh
cat evidence/platform-delivery/ecs-containers.txt
cat evidence/platform-delivery/smoke-version.json
```

✅ `pos` image should look like `.../pos@sha256:...` or `...: <40-char-sha>`.  
✅ Both containers RUNNING.

Optional (nice for trainer): save apply notes:

```bash
# if you still have terminal scrollback from bootstrap/apply, paste into:
# evidence/platform-delivery/bootstrap-apply.txt
```

---

## STEP 5 — Commit + push + open PR

```bash
cd ~/devops-g-9-tillflow
git add -A
git status

git commit -m "$(cat <<'EOF'
feat(platform): G1 golden path, OIDC CI, scans, SHA deploy evidence

Close G0 follow-ups and land sandbox ECS (pos+ADOT), digest deploy
scripts, gitleaks/trivy PR checks, and platform evidence pack.
EOF
)"

git push -u origin platform/g1-foundation

gh pr create --title "G1 Platform — ECS golden path + OIDC CI" --body "$(cat <<'EOF'
## Summary
- G0 follow-ups: ownership cleanup, ADR 0005, stubs 0006/0007
- VPC/ECR/ALB/ECS in eu-north-1; POS + ADOT sidecar
- SHA/digest deploy; /health /ready /version smoke
- OIDC role + PR checks (gitleaks, Trivy, terraform plan)
- Evidence in evidence/platform-delivery/

## Test plan
- [x] curl /health /ready 200
- [x] ECS 2 containers RUNNING
- [x] collect.sh artifacts
- [ ] CI green on this PR
- [ ] Joy review
EOF
)"
```

✅ Copy the PR URL.

---

## STEP 6 — Slack teammates

```text
Alice + Joy — G1 PR is up: <PR_URL>

1) Reply signed 2026-09-16 for ownership.md
2) Joy — please review the PR (default infra reviewer)
3) Joy — fill docs/adrs/0006-idempotency-replay.md (your PR ok)
4) Alice — fill docs/adrs/0007-multi-tenancy.md + open any Product PR
```

When they reply, update sign-off dates in `docs/ownership.md` and push.

---

## STEP 7 — Slack trainer

```text
Hey — Group 9 G1 ready for review.
PR: <PR_URL>
eu-north-1 · /health OK · ECS pos+adot RUNNING · evidence/platform-delivery/
G0 follow-ups included (ADR 0005; 0006/0007 stubbed for Joy/Alice).
```

---

## Done when

- [ ] SHA deploy + `/version` shows commit  
- [ ] Evidence refreshed  
- [ ] GitHub vars + `sandbox` env  
- [ ] PR open, CI running, Joy reviewing  
- [ ] Trainer pinged  
- [ ] Sign-offs when teammates reply  
