# Scheduled reconcile pass (ADR 0009 G3-10). Nothing else runs Payments' sweep on a schedule, so
# UNKNOWN payments and payouts only resolved when someone called it by hand; reconcile-stale
# (app_alarms.tf) watches this.
#
# Each Payments task keeps its own SQLite database, so the pass has to run inside the service:
# a Lambda in the private subnets calls POST /_admin/sweep on the internal ALB every 5 minutes.
# The schedule can be disabled (state = "DISABLED") to stop the pass on purpose, which is how the
# G3 Slack drill trips reconcile-stale for a real reason.

resource "aws_security_group" "reconcile_sweep" {
  name        = "${var.name_prefix}-reconcile-sweep"
  description = "Scheduled reconcile Lambda: HTTP to the internal ALB only"
  vpc_id      = aws_vpc.main.id

  egress {
    description = "HTTP to internal ALB"
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = [var.vpc_cidr]
  }

  tags = {
    Name    = "${var.name_prefix}-reconcile-sweep"
    service = "payments"
  }
}

resource "aws_iam_role" "reconcile_sweep" {
  name = "${var.name_prefix}-reconcile-sweep"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action    = "sts:AssumeRole"
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
    }]
  })

  tags = {
    Name    = "${var.name_prefix}-reconcile-sweep"
    service = "payments"
  }
}

resource "aws_iam_role_policy" "reconcile_sweep" {
  name = "${var.name_prefix}-reconcile-sweep"
  role = aws_iam_role.reconcile_sweep.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "OwnLogs"
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "${aws_cloudwatch_log_group.reconcile_sweep.arn}:*"
      },
      {
        # Lambda needs these to attach its network interfaces in the private subnets. AWS does not
        # support resource-level scoping for them.
        Sid    = "VpcNetworkInterfaces"
        Effect = "Allow"
        Action = [
          "ec2:CreateNetworkInterface",
          "ec2:DescribeNetworkInterfaces",
          "ec2:DeleteNetworkInterface",
          "ec2:AssignPrivateIpAddresses",
          "ec2:UnassignPrivateIpAddresses"
        ]
        Resource = "*"
      }
    ]
  })
}

resource "aws_cloudwatch_log_group" "reconcile_sweep" {
  name              = "/aws/lambda/${var.name_prefix}-reconcile-sweep"
  retention_in_days = 14

  tags = {
    Name    = "/aws/lambda/${var.name_prefix}-reconcile-sweep"
    service = "payments"
  }
}

data "archive_file" "reconcile_sweep" {
  type        = "zip"
  source_file = "${path.module}/lambda/reconcile_sweep/index.py"
  output_path = "${path.module}/.build/reconcile_sweep.zip"
  # Same bytes on a laptop (umask 0002) and in CI (0022): a rebuild from a laptop must not
  # leave every Lambda looking changed to the next CI plan.
  output_file_mode = "0644"
}

resource "aws_lambda_function" "reconcile_sweep" {
  function_name    = "${var.name_prefix}-reconcile-sweep"
  description      = "Calls Payments POST /_admin/sweep on the internal ALB (ADR 0009 G3-10)"
  role             = aws_iam_role.reconcile_sweep.arn
  filename         = data.archive_file.reconcile_sweep.output_path
  source_code_hash = data.archive_file.reconcile_sweep.output_base64sha256
  handler          = "index.handler"
  runtime          = "python3.12"
  timeout          = 60
  memory_size      = 128

  vpc_config {
    subnet_ids         = aws_subnet.private[*].id
    security_group_ids = [aws_security_group.reconcile_sweep.id]
  }

  environment {
    variables = {
      SWEEP_URL = "http://${aws_lb.main.dns_name}/_admin/sweep"
    }
  }

  tags = {
    Name    = "${var.name_prefix}-reconcile-sweep"
    service = "payments"
  }

  depends_on = [
    aws_iam_role_policy.reconcile_sweep,
    aws_cloudwatch_log_group.reconcile_sweep,
  ]
}

resource "aws_iam_role" "reconcile_scheduler" {
  name = "${var.name_prefix}-reconcile-scheduler"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action    = "sts:AssumeRole"
      Effect    = "Allow"
      Principal = { Service = "scheduler.amazonaws.com" }
      Condition = {
        StringEquals = { "aws:SourceAccount" = local.account_id }
      }
    }]
  })

  tags = {
    Name    = "${var.name_prefix}-reconcile-scheduler"
    service = "payments"
  }
}

resource "aws_iam_role_policy" "reconcile_scheduler" {
  name = "${var.name_prefix}-reconcile-scheduler"
  role = aws_iam_role.reconcile_scheduler.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid      = "InvokeSweep"
      Effect   = "Allow"
      Action   = ["lambda:InvokeFunction"]
      Resource = aws_lambda_function.reconcile_sweep.arn
    }]
  })
}

resource "aws_scheduler_schedule" "reconcile_sweep" {
  name                = "${var.name_prefix}-reconcile-sweep"
  description         = "Payments reconcile pass every 5 minutes (ADR 0009 G3-10)"
  schedule_expression = "rate(5 minutes)"
  state               = var.reconcile_sweep_enabled ? "ENABLED" : "DISABLED"

  flexible_time_window {
    mode = "OFF"
  }

  target {
    arn      = aws_lambda_function.reconcile_sweep.arn
    role_arn = aws_iam_role.reconcile_scheduler.arn

    retry_policy {
      maximum_retry_attempts = 0
    }
  }
}
