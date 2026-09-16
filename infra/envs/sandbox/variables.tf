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

variable "pos_image_tag" {
  type        = string
  description = "Immutable tag for pos image (use git SHA in CI; bootstrap for first push)"
  default     = "bootstrap"
}

variable "pos_container_port" {
  type    = number
  default = 8080
}

variable "github_org" {
  type    = string
  default = "emebetgirmay"
}

variable "github_repo" {
  type    = string
  default = "devops-g-9-tillflow"
}
