# Fill after bootstrap `terraform output`.
# Then: terraform init -backend-config=backend.hcl

bucket         = "devops-g9-tfstate-240462142849"
key            = "sandbox/terraform.tfstate"
region         = "eu-north-1"
dynamodb_table = "devops-g9-tflock"
encrypt        = true
