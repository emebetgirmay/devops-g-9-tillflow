# Terraform — devops-g9

## Layout

```
infra/
├─ bootstrap/       # S3 tfstate + DynamoDB lock (apply FIRST, local state)
├─ envs/sandbox/    # VPC (+ later ECS/RDS) — remote backend
├─ modules/         # reusable modules (grow as needed)
└─ .gitignore
```

## Region / account

`eu-north-1` · prefix `devops-g9` · account `240462142849`

## Apply order

```bash
# 1) Bootstrap
cd infra/bootstrap
terraform init && terraform plan && terraform apply

# 2) Sandbox
cd ../envs/sandbox
terraform init -backend-config=backend.hcl
terraform plan && terraform apply
```

## G1 golden path (after VPC exists)

```bash
cd infra/envs/sandbox
terraform apply                          # ECR, ALB, ECS, IAM, logs

cd ../../..
./scripts/build-push-pos.sh bootstrap    # push stub image

cd infra/envs/sandbox
terraform apply -var=pos_image_tag=bootstrap
# if tasks were already failing on missing image:
aws ecs update-service --cluster devops-g9 --service devops-g9-pos --force-new-deployment

terraform output health_url
curl -sS "$(terraform output -raw health_url)"
```

Then: GHA `terraform plan` on PR + evidence pack.
