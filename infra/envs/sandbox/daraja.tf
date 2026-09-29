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
# Only the Payments execution role can read the secret; Commission and CI never can. If the
# secret is ever recreated, update daraja_secret_arn (its ARN suffix changes).

variable "payments_mpesa_adapter" {
  type        = string
  description = "Payments M-Pesa adapter: fake or daraja_sandbox (B2C only; STK charges are declined)"
  default     = "daraja_sandbox"

  validation {
    condition     = contains(["fake", "daraja_sandbox"], var.payments_mpesa_adapter)
    error_message = "payments_mpesa_adapter must be fake or daraja_sandbox."
  }
}

variable "daraja_secret_arn" {
  type        = string
  description = "Full ARN of the hand-made devops-g9/daraja secret. Passed in, not looked up, so Terraform and CI never call Secrets Manager."
  default     = "arn:aws:secretsmanager:eu-north-1:240462142849:secret:devops-g9/daraja-XozcUR"
}

variable "daraja_base_url" {
  type        = string
  description = "Daraja sandbox base URL (https, host starting sandbox.); Payments refuses anything else"
  default     = "https://sandbox.safaricom.co.ke"
}

variable "daraja_callback_ips" {
  type        = list(string)
  description = "Provider result-callback source IPs, observed in Payments logs or copied from Daraja docs. Never guessed. Empty rejects all outside callbacks."
  # Observed 2026-09-29 as the source of the sandbox B2C result for evidence/daraja-b2c-contract run 1.
  # Daraja may send from other addresses too; add each one only after it shows up in the log.
  default = ["196.201.212.69"]
}

locals {
  source_ip_header = "x-tillflow-source-ip"
  payments_daraja  = var.payments_mpesa_adapter == "daraja_sandbox"
  daraja_secret    = var.daraja_secret_arn

  payments_daraja_environment = local.payments_daraja ? [
    { name = "MPESA_BASE_URL", value = var.daraja_base_url },
    { name = "MPESA_CALLBACK_BASE_URL", value = aws_apigatewayv2_api.app.api_endpoint },
    # Empty list: keep Payments' localhost-only default, so outside callbacks are refused and logged.
    { name = "CALLBACK_ALLOWED_IPS", value = length(var.daraja_callback_ips) > 0 ? join(",", var.daraja_callback_ips) : "127.0.0.1,::1" },
    # api_gateway.tf overwrites this header with the caller's address on every request.
    { name = "CALLBACK_SOURCE_HEADER", value = local.source_ip_header },
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
