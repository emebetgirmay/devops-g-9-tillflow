# G3 Slack path (ADR 0010 section 4):
#   CloudWatch alarm (alarm_actions AND ok_actions) -> SNS devops-g9-alerts -> Lambda -> Slack.
# The webhook lives only in Secrets Manager. Terraform creates the secret with NO value; a member
# sets it out of band (docs/runbook.md, "Slack webhook"). CI never reads or writes the value.

resource "aws_secretsmanager_secret" "slack" {
  name        = "${var.name_prefix}/slack-webhook"
  description = "Slack incoming webhook for alerts, as {\"url\": \"...\"}. Value set by hand, never in Git."
  # 0, not 7: a rebuild recreates this name at once (a secret pending deletion blocks the name).
  # The webhook goes with it and is set again by hand after a rebuild (runbook "Slack webhook").
  recovery_window_in_days = 0

  tags = {
    Name    = "${var.name_prefix}/slack-webhook"
    service = "reliability"
  }
}

# Customer-managed key: CloudWatch cannot publish to a topic encrypted with alias/aws/sns.
data "aws_iam_policy_document" "alerts_kms" {
  statement {
    sid       = "AccountAdmin"
    actions   = ["kms:*"]
    resources = ["*"]
    principals {
      type        = "AWS"
      identifiers = ["arn:aws:iam::${local.account_id}:root"]
    }
  }

  statement {
    sid       = "AlarmPublishers"
    actions   = ["kms:Decrypt", "kms:GenerateDataKey*"]
    resources = ["*"]
    principals {
      type        = "Service"
      identifiers = ["cloudwatch.amazonaws.com", "sns.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account_id]
    }
  }
}

resource "aws_kms_key" "alerts" {
  description             = "Encrypts SNS topic ${var.name_prefix}-alerts"
  deletion_window_in_days = 7
  enable_key_rotation     = true
  policy                  = data.aws_iam_policy_document.alerts_kms.json

  tags = {
    Name    = "${var.name_prefix}-alerts"
    service = "reliability"
  }
}

resource "aws_kms_alias" "alerts" {
  name          = "alias/${var.name_prefix}-alerts"
  target_key_id = aws_kms_key.alerts.key_id
}

resource "aws_sns_topic" "alerts" {
  name              = "${var.name_prefix}-alerts"
  kms_master_key_id = aws_kms_key.alerts.arn

  tags = {
    Name    = "${var.name_prefix}-alerts"
    service = "reliability"
  }
}

data "aws_iam_policy_document" "alerts_topic" {
  statement {
    sid       = "CloudWatchAlarmsPublish"
    actions   = ["sns:Publish"]
    resources = [aws_sns_topic.alerts.arn]
    principals {
      type        = "Service"
      identifiers = ["cloudwatch.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account_id]
    }
  }
}

resource "aws_sns_topic_policy" "alerts" {
  arn    = aws_sns_topic.alerts.arn
  policy = data.aws_iam_policy_document.alerts_topic.json
}

# Notifier Lambda: reads one secret, writes its own logs. Nothing else.
resource "aws_iam_role" "slack_notifier" {
  name = "${var.name_prefix}-slack-notifier"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action    = "sts:AssumeRole"
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
    }]
  })

  tags = {
    Name    = "${var.name_prefix}-slack-notifier"
    service = "reliability"
  }
}

resource "aws_iam_role_policy" "slack_notifier" {
  name = "${var.name_prefix}-slack-notifier"
  role = aws_iam_role.slack_notifier.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "ReadSlackWebhook"
        Effect   = "Allow"
        Action   = ["secretsmanager:GetSecretValue"]
        Resource = aws_secretsmanager_secret.slack.arn
      },
      {
        Sid      = "OwnLogs"
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "${aws_cloudwatch_log_group.slack_notifier.arn}:*"
      }
    ]
  })
}

resource "aws_cloudwatch_log_group" "slack_notifier" {
  name              = "/aws/lambda/${var.name_prefix}-slack-notifier"
  retention_in_days = 14

  tags = {
    Name    = "/aws/lambda/${var.name_prefix}-slack-notifier"
    service = "reliability"
  }
}

# Zipped at plan time. release.yml uploads .build/ with the saved plan so the apply job has it.
data "archive_file" "slack_notifier" {
  type        = "zip"
  source_file = "${path.module}/lambda/slack_notifier/index.py"
  output_path = "${path.module}/.build/slack_notifier.zip"
}

resource "aws_lambda_function" "slack_notifier" {
  function_name    = "${var.name_prefix}-slack-notifier"
  description      = "CloudWatch alarm (SNS) to Slack, per the runbook alert contract"
  role             = aws_iam_role.slack_notifier.arn
  filename         = data.archive_file.slack_notifier.output_path
  source_code_hash = data.archive_file.slack_notifier.output_base64sha256
  handler          = "index.handler"
  runtime          = "python3.12"
  timeout          = 15
  memory_size      = 128

  environment {
    variables = {
      SLACK_SECRET_ID = aws_secretsmanager_secret.slack.name
      ENVIRONMENT     = var.environment
      GRAFANA_URL     = var.grafana_url
      RUNBOOK_URL     = "https://github.com/${var.github_org}/${var.github_repo}/blob/main/docs/runbook.md"
    }
  }

  tags = {
    Name    = "${var.name_prefix}-slack-notifier"
    service = "reliability"
  }

  depends_on = [
    aws_iam_role_policy.slack_notifier,
    aws_cloudwatch_log_group.slack_notifier,
  ]
}

resource "aws_sns_topic_subscription" "slack_notifier" {
  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "lambda"
  endpoint  = aws_lambda_function.slack_notifier.arn
}

resource "aws_lambda_permission" "slack_notifier_sns" {
  statement_id  = "AllowSnsAlerts"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.slack_notifier.function_name
  principal     = "sns.amazonaws.com"
  source_arn    = aws_sns_topic.alerts.arn
}
