variable "aws_region" {
  type        = string
  description = "Assigned AWS region for devops-g9"
  default     = "eu-north-1"
}

variable "name_prefix" {
  type        = string
  description = "Resource name prefix"
  default     = "devops-g9"
}

variable "owner" {
  type        = string
  description = "Platform DRI GitHub handle"
  default     = "emebetgirmay"
}

variable "environment" {
  type        = string
  description = "Environment name"
  default     = "sandbox"
}
