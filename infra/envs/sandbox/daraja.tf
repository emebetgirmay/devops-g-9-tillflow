# Daraja sandbox wiring for Payments (ADR 0004, ADR 0008). Off by default: with
# payments_mpesa_adapter = "fake" nothing here is created and the task definition is unchanged.
#
# Turning it on (reviewed PR changing the defaults below, then the gated apply):
#   1. Create the secret by hand; its value never goes through Terraform or git:
#        aws secretsmanager create-secret --name devops-g9/daraja --secret-string file://daraja.json
#      with keys consumer_key, consumer_secret, b2c_shortcode, b2c_initiator_name,
#      b2c_security_credential (the initiator password already encrypted with the sandbox cert).
#   2. Set payments_mpesa_adapter = "daraja_sandbox" and daraja_base_url.
#   3. daraja_callback_ips may start empty: Payments then rejects every outside result callback but
#      logs its source ("result from disallowed source <ip>"). Add the observed provider address in a
#      follow-up PR; never a guessed one.
# Only the Payments execution role can read the secret; Commission and CI never can.

variable "payments_mpesa_adapter" {
  type        = string
  description = "Payments M-Pesa adapter: fake or daraja_sandbox (B2C only; STK charges are declined)"
  default     = "daraja_sandbox"

  validation {
    condition     = contains(["fake", "daraja_sandbox"], var.payments_mpesa_adapter)
    error_message = "payments_mpesa_adapter must be fake or daraja_sandbox."
  }
}

variable "daraja_secret_name" {
  type        = string
  description = "Secrets Manager secret holding the Daraja sandbox B2C credentials (created by hand)"
  default     = "devops-g9/daraja"
}

variable "daraja_base_url" {
  type        = string
  description = "Daraja sandbox base URL (https, host starting sandbox.); Payments refuses anything else"
  default     = "https://sandbox.safaricom.co.ke"
}

variable "daraja_callback_ips" {
  type        = list(string)
  description = "Provider result-callback source IPs, observed in Payments logs or copied from Daraja docs. Never guessed. Empty rejects all outside callbacks."
  default     = []
}

variable "payments_trusted_proxy_hops" {
  type        = number
  description = "Proxies in front of Payments that append X-Forwarded-For (API Gateway, then the ALB). Verify live: see README."
  default     = 2
}

locals {
  payments_daraja = var.payments_mpesa_adapter == "daraja_sandbox"
  daraja_secret   = local.payments_daraja ? data.aws_secretsmanager_secret.daraja[0].arn : ""

  payments_daraja_environment = local.payments_daraja ? [
    { name = "MPESA_BASE_URL", value = var.daraja_base_url },
    { name = "MPESA_CALLBACK_BASE_URL", value = aws_apigatewayv2_api.app.api_endpoint },
    # Empty list: keep Payments' localhost-only default, so outside callbacks are refused and logged.
    { name = "CALLBACK_ALLOWED_IPS", value = length(var.daraja_callback_ips) > 0 ? join(",", var.daraja_callback_ips) : "127.0.0.1,::1" },
    { name = "TRUSTED_PROXY_HOPS", value = tostring(var.payments_trusted_proxy_hops) },
  ] : []

  payments_daraja_secrets = [
    for env_name, key in {
      MPESA_CONSUMER_KEY            = "consumer_key"
      MPESA_CONSUMER_SECRET         = "consumer_secret"
      MPESA_B2C_SHORTCODE           = "b2c_shortcode"
      MPESA_B2C_INITIATOR_NAME      = "b2c_initiator_name"
      MPESA_B2C_SECURITY_CREDENTIAL = "b2c_security_credential"
    } : { name = env_name, valueFrom = "${local.daraja_secret}:${key}::" }
  ]
}

data "aws_secretsmanager_secret" "daraja" {
  count = local.payments_daraja ? 1 : 0
  name  = var.daraja_secret_name
}

resource "aws_iam_role_policy" "payments_exec_daraja" {
  count = local.payments_daraja ? 1 : 0
  name  = "${var.name_prefix}-payments-daraja-secret"
  role  = aws_iam_role.payments_exec.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["secretsmanager:GetSecretValue"]
      Resource = [local.daraja_secret]
    }]
  })
}
