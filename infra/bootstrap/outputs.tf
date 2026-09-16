output "aws_region" {
  value = var.aws_region
}

output "account_id" {
  value = local.account_id
}

output "state_bucket" {
  value = aws_s3_bucket.tfstate.id
}

output "lock_table" {
  value = aws_dynamodb_table.tflock.name
}

output "backend_hcl_example" {
  description = "Paste into envs/sandbox/backend.hcl after bootstrap"
  value       = <<-EOT
    bucket         = "${aws_s3_bucket.tfstate.id}"
    key            = "sandbox/terraform.tfstate"
    region         = "${var.aws_region}"
    dynamodb_table = "${aws_dynamodb_table.tflock.name}"
    encrypt        = true
  EOT
}
