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

## Daraja sandbox B2C (off by default)

`envs/sandbox/daraja.tf` wires Payments to the Daraja sandbox for payouts only. With the default
`payments_mpesa_adapter = "fake"` it creates nothing and changes no task definition. To turn it on:

1. Create `devops-g9/daraja` in Secrets Manager by hand (keys: `consumer_key`, `consumer_secret`,
   `b2c_shortcode`, `b2c_initiator_name`, `b2c_security_credential`). The value never goes through
   Terraform, git or CI; only the Payments execution role can read it.
2. In a reviewed PR set `payments_mpesa_adapter = "daraja_sandbox"`, `daraja_base_url` and
   `daraja_callback_ips` (Daraja's documented result IPs, copied from the portal). The gated apply
   refuses if either is missing. The release pipeline then deploys a task with those settings.
3. **Before trusting the allowlist**, check `payments_trusted_proxy_hops` (default 2: API Gateway
   then the ALB each append to `X-Forwarded-For`). From a laptop, POST `{}` to
   `<api endpoint>/payments/daraja/b2c-callback`: it must answer 403 `source_not_allowed`, and the
   Payments log line `result from disallowed source <ip>` must show your public IP. A `10.x`
   address means the hop count is wrong; fix it before sending a payout.

While it is on, STK charges through Payments are declined at initiation (the real adapter's STK
path is not built), so POS's sale flow stops. Switch back to `fake` after the B2C contract test.
