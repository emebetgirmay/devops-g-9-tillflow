variable "aws_region" {
  type    = string
  default = "eu-north-1"
}

variable "name_prefix" {
  type    = string
  default = "devops-g9"
}

variable "owner" {
  type    = string
  default = "emebetgirmay"
}

variable "environment" {
  type    = string
  default = "sandbox"
}

variable "vpc_cidr" {
  type    = string
  default = "10.9.0.0/16"
}

variable "pos_container_port" {
  type    = number
  default = 8080
}

variable "payments_container_port" {
  type    = number
  default = 8080
}

# When null/empty, ECS uses a self-healthy busybox placeholder (P0-3).
# Pipeline deploys real digests; service ignore_changes keeps them.
variable "pos_image_digest" {
  type        = string
  description = "Optional ECR image URI including @sha256 digest for POS app"
  default     = null
  nullable    = true
}

variable "payments_image_digest" {
  type        = string
  description = "Optional ECR image URI including @sha256 digest for Payments app"
  default     = null
  nullable    = true
}

variable "adot_collector_image" {
  type    = string
  default = "public.ecr.aws/aws-observability/aws-otel-collector:v0.43.1"
}

variable "github_org" {
  type    = string
  default = "emebetgirmay"
}

variable "github_repo" {
  type    = string
  default = "devops-g-9-tillflow"
}

variable "github_owner_id" {
  type    = string
  default = "199029553"
}

variable "github_repo_id" {
  type    = string
  default = "1362917698"
}

# Grafana Cloud stack (ADR 0010). The URL goes into alert links. The account ID and external ID
# are what the stack's CloudWatch data source shows under "Grafana Assume Role"; neither is a
# secret, they only say which stack may assume devops-g9-grafana-read.
variable "grafana_url" {
  type    = string
  default = "https://honestmesa567.grafana.net"
}

variable "grafana_cloud_aws_account_id" {
  type    = string
  default = "008923505280"
}

variable "grafana_external_id" {
  type    = string
  default = "1848856"
}

# Scheduled reconcile pass (reconcile.tf). Set false only to stop the pass on purpose, e.g. the G3
# Slack drill; reconcile-stale fires within 15 minutes while it is off.
variable "reconcile_sweep_enabled" {
  type = bool
  # G3 Slack drill (ADR 0010 section 4): the sweep is stopped on purpose so reconcile-stale fires
  # for a real reason. Revert to true right after the FIRING message is captured.
  default = false
}
