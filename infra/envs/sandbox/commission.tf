# Commission (ADR 0008): scheduled one-off ECS tasks, not a service. Same image, three schedules
# in EAT (Africa/Nairobi):
#   close      00:15 daily     close.py: yesterday's confirmed-paid sales into the payout ledger
#   disburse   every 10 min, 00:00-05:59   disburse.py: send PLANNED rows to Payments, reconcile
#   check      06:30 daily     disburse.py --check: count payouts not terminal (the 06:30 SLO)
#
# The ledger is on RDS (schema commission, secret devops-g9/db/commission). The release pipeline
# builds the image and registers a new revision of this task definition with it; the schedules run
# the family's latest revision, so a release reaches the next run without touching them.
#
# Schedules are off until an image has been released and the tenant list is set (a reviewed PR):
# close.py refuses to run without tenants. The check schedule waits for disburse.py --check.

variable "commission_schedules_enabled" {
  description = "Turn the close and disburse schedules on (after the first Commission release)."
  type        = bool
  default     = false
}

variable "commission_check_enabled" {
  description = "Turn the 06:30 EAT check schedule on (after disburse.py --check exists)."
  type        = bool
  default     = true # disburse.py --check released in #88
}

variable "commission_tenant_ids" {
  description = "Tenants close.py closes each day, comma-separated (ADR decision: task environment, not a POS endpoint, until onboarding is dynamic)."
  type        = string
  default     = ""
}

locals {
  commission_family = "${var.name_prefix}-commission"
  # The family ARN without a revision: EventBridge Scheduler runs its latest ACTIVE revision.
  commission_family_arn = "arn:aws:ecs:${var.aws_region}:${local.account_id}:task-definition/${local.commission_family}"
  commission_schedules = merge(
    var.commission_schedules_enabled ? {
      close    = { cron = "cron(15 0 * * ? *)", command = ["close.py"], what = "Close yesterday (EAT) into the payout ledger" }
      disburse = { cron = "cron(0/10 0-5 * * ? *)", command = ["disburse.py"], what = "Send planned payouts to Payments and reconcile" }
    } : {},
    var.commission_check_enabled ? {
      check = { cron = "cron(30 6 * * ? *)", command = ["disburse.py", "--check"], what = "Count payouts not terminal by 06:30 EAT" }
    } : {},
  )
}

resource "aws_ecr_repository" "commission" {
  name                 = "${var.name_prefix}/commission"
  image_tag_mutability = "IMMUTABLE"
  force_delete         = true # images are rebuilt from git by the release

  image_scanning_configuration {
    scan_on_push = true
  }

  encryption_configuration {
    encryption_type = "AES256"
  }

  tags = {
    Name    = "${var.name_prefix}/commission"
    service = "commission"
  }
}

resource "aws_ecr_lifecycle_policy" "commission" {
  repository = aws_ecr_repository.commission.name

  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Keep the newest 10 images"
      selection = {
        tagStatus   = "any"
        countType   = "imageCountMoreThan"
        countNumber = 10
      }
      action = { type = "expire" }
    }]
  })
}

resource "aws_cloudwatch_log_group" "commission" {
  name              = "/${var.name_prefix}/commission"
  retention_in_days = 14

  tags = {
    Name    = "/${var.name_prefix}/commission"
    service = "commission"
  }
}

resource "aws_security_group" "commission" {
  name        = "${var.name_prefix}-commission"
  description = "Commission tasks: POS and Payments via the internal ALB, RDS, HTTPS via NAT"
  vpc_id      = aws_vpc.main.id

  egress {
    description = "HTTP to internal ALB (POS paid sales, Payments payouts)"
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = [var.vpc_cidr]
  }

  egress {
    description = "PostgreSQL to RDS"
    from_port   = 5432
    to_port     = 5432
    protocol    = "tcp"
    cidr_blocks = [var.vpc_cidr]
  }

  # Accepted in .trivyignore (AVD-AWS-0104), same as the service tasks.
  egress {
    description = "HTTPS via NAT (image pull, Secrets Manager, CloudWatch Logs)"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Name    = "${var.name_prefix}-commission"
    service = "commission"
  }
}

resource "aws_iam_role" "commission_exec" {
  name = "${var.name_prefix}-commission-exec"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action    = "sts:AssumeRole"
      Effect    = "Allow"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
    }]
  })

  tags = {
    Name    = "${var.name_prefix}-commission-exec"
    service = "commission"
  }
}

resource "aws_iam_role_policy_attachment" "commission_exec" {
  role       = aws_iam_role.commission_exec.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

resource "aws_iam_role_policy" "commission_exec_db" {
  name = "${var.name_prefix}-commission-db-secret"
  role = aws_iam_role.commission_exec.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["secretsmanager:GetSecretValue"]
      Resource = [aws_secretsmanager_secret.db["commission"].arn]
    }]
  })
}

resource "aws_ecs_task_definition" "commission" {
  family                   = local.commission_family
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = "256"
  memory                   = "512"
  execution_role_arn       = aws_iam_role.commission_exec.arn

  container_definitions = jsonencode([
    {
      name      = "commission"
      image     = "${aws_ecr_repository.commission.repository_url}:placeholder" # the release registers the real digest
      essential = true
      # Ledger on RDS; the image's /app/data SQLite default is never used when deployed.
      readonlyRootFilesystem = true
      user                   = "10001:10001"
      environment = [
        { name = "POS_BASE_URL", value = local.payments_base_url }, # internal ALB: default route is POS
        { name = "PAYMENTS_BASE_URL", value = local.payments_base_url },
        { name = "COMMISSION_TENANT_IDS", value = var.commission_tenant_ids },
      ]
      secrets = [
        { name = "DATABASE_URL", valueFrom = "${aws_secretsmanager_secret.db["commission"].arn}:url::" },
      ]
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.commission.name
          "awslogs-region"        = var.aws_region
          "awslogs-stream-prefix" = "commission"
        }
      }
    }
  ])

  tags = {
    Name    = local.commission_family
    service = "commission"
  }
}

# --- Schedules --------------------------------------------------------------------------------

resource "aws_iam_role" "commission_scheduler" {
  name = "${var.name_prefix}-commission-scheduler"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action    = "sts:AssumeRole"
      Effect    = "Allow"
      Principal = { Service = "scheduler.amazonaws.com" }
      Condition = { StringEquals = { "aws:SourceAccount" = local.account_id } }
    }]
  })

  tags = {
    Name    = "${var.name_prefix}-commission-scheduler"
    service = "commission"
  }
}

resource "aws_iam_role_policy" "commission_scheduler" {
  name = "${var.name_prefix}-commission-scheduler"
  role = aws_iam_role.commission_scheduler.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect    = "Allow"
        Action    = ["ecs:RunTask"]
        Resource  = ["${local.commission_family_arn}:*", local.commission_family_arn]
        Condition = { ArnEquals = { "ecs:cluster" = aws_ecs_cluster.main.arn } }
      },
      {
        Effect   = "Allow"
        Action   = ["iam:PassRole"]
        Resource = [aws_iam_role.commission_exec.arn]
      },
    ]
  })
}

resource "aws_scheduler_schedule" "commission" {
  for_each = local.commission_schedules

  name                         = "${var.name_prefix}-commission-${each.key}"
  description                  = each.value.what
  schedule_expression          = each.value.cron
  schedule_expression_timezone = "Africa/Nairobi"

  flexible_time_window {
    mode = "OFF"
  }

  target {
    arn      = aws_ecs_cluster.main.arn
    role_arn = aws_iam_role.commission_scheduler.arn

    ecs_parameters {
      task_definition_arn = local.commission_family_arn
      launch_type         = "FARGATE"
      task_count          = 1

      network_configuration {
        subnets          = aws_subnet.private[*].id
        security_groups  = [aws_security_group.commission.id]
        assign_public_ip = false
      }

    }

    input = jsonencode({
      containerOverrides = [{ name = "commission", command = each.value.command }]
    })

    retry_policy {
      maximum_retry_attempts = 0 # every pass is idempotent and the next one comes in 10 minutes
    }
  }
}

# --- The 06:30 EAT SLO (docs/slo-error-budgets.md, Commission) ------------------------------
# disburse.py --check logs one JSON line {"event": "payouts_not_terminal", "count": N}; this
# filter turns it into a metric, so Commission needs no AWS SDK or permission to publish it.

resource "aws_cloudwatch_log_metric_filter" "commission_not_terminal" {
  name           = "${var.name_prefix}-commission-payouts-not-terminal"
  log_group_name = aws_cloudwatch_log_group.commission.name
  pattern        = "{ $.event = \"payouts_not_terminal\" }"

  metric_transformation {
    name      = "CommissionPayoutsNotTerminal"
    namespace = "TillFlow/Commission"
    value     = "$.count"
  }
}

resource "aws_cloudwatch_metric_alarm" "commission_payouts_late" {
  count = var.commission_check_enabled ? 1 : 0

  alarm_name          = "${var.name_prefix}-commission-payouts-late"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "CommissionPayoutsNotTerminal"
  namespace           = "TillFlow/Commission"
  statistic           = "Maximum"
  period              = 3600
  threshold           = 0
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alerts.arn]
  ok_actions          = [aws_sns_topic.alerts.arn]

  alarm_description = jsonencode({
    environment       = var.environment
    service           = "commission"
    symptom           = "Payouts not terminal at 06:30 EAT"
    slo_impact        = "Commission SLO: eligible payouts terminal by 06:30 EAT, 99%"
    observed          = "CommissionPayoutsNotTerminal > 0 from disburse.py --check"
    grafana_panel     = local.grafana_dashboard
    runbook           = "${local.runbook_url}#commission-payouts-late"
    owner             = "@chesangJ"
    first_safe_action = "Run disburse.py once more (it only reconciles); never re-send a payout by hand"
  })

  tags = { service = "commission" }
}

output "commission_task_definition_arn" {
  value = aws_ecs_task_definition.commission.arn
}
