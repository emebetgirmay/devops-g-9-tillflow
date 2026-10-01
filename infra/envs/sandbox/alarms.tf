# G3 platform alarms (ADR 0010 section 3), all on SNS devops-g9-alerts (alerts.tf) for both
# ALARM and OK, so Slack shows firing AND recovered. Each description is the runbook's alert
# contract as JSON; the notifier Lambda formats it.
#
# Burn alarms read the ALB per target group until the services' own SLI metrics exist, then
# move to them (ADR 0010 R-6). Fast burn = 14.4 x (1 - target) over 5 min, pages;
# slow burn = 6 x (1 - target) over 30 min, Slack only.

locals {
  runbook_url       = "https://github.com/${var.github_org}/${var.github_repo}/blob/main/docs/runbook.md"
  grafana_dashboard = var.grafana_url == "" ? "" : "${var.grafana_url}/d/tillflow-overview"

  slo_services = {
    pos = {
      target_group = aws_lb_target_group.pos.arn_suffix
      slo          = 0.999
      budget       = "28d budget 0.1% (40m 19s)"
      owner        = "@Moraaalice"
    }
    payments = {
      target_group = aws_lb_target_group.payments.arn_suffix
      slo          = 0.995
      budget       = "28d budget 0.5% (3h 21m 36s)"
      owner        = "@chesangJ"
    }
  }

  burn_windows = {
    fast = { factor = 14.4, period = 300, severity = "page" }
    slow = { factor = 6, period = 1800, severity = "slack" }
  }

  burn_alarms = merge([
    for svc, s in local.slo_services : {
      for window, w in local.burn_windows : "${svc}-${window}-burn" => {
        service      = svc
        window       = window
        target_group = s.target_group
        threshold    = w.factor * (1 - s.slo)
        period       = w.period
        severity     = w.severity
        budget       = s.budget
        owner        = s.owner
        slo          = s.slo
      }
    }
  ]...)
}

# --- Edge probe: public API Gateway /ready every minute ------------------------------------

resource "aws_iam_role" "probe" {
  name = "${var.name_prefix}-probe"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action    = "sts:AssumeRole"
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
    }]
  })

  tags = {
    Name    = "${var.name_prefix}-probe"
    service = "reliability"
  }
}

resource "aws_iam_role_policy" "probe" {
  name = "${var.name_prefix}-probe"
  role = aws_iam_role.probe.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "ProbeMetricsOnly"
        Effect   = "Allow"
        Action   = ["cloudwatch:PutMetricData"]
        Resource = "*"
        Condition = {
          StringEquals = { "cloudwatch:namespace" = "TillFlow/Probe" }
        }
      },
      {
        Sid      = "OwnLogs"
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "${aws_cloudwatch_log_group.probe.arn}:*"
      }
    ]
  })
}

resource "aws_cloudwatch_log_group" "probe" {
  name              = "/aws/lambda/${var.name_prefix}-probe"
  retention_in_days = 14

  tags = {
    Name    = "/aws/lambda/${var.name_prefix}-probe"
    service = "reliability"
  }
}

data "archive_file" "probe" {
  type        = "zip"
  source_file = "${path.module}/lambda/probe/index.py"
  output_path = "${path.module}/.build/probe.zip"
  # Same bytes on a laptop (umask 0002) and in CI (0022): a rebuild from a laptop must not
  # leave every Lambda looking changed to the next CI plan.
  output_file_mode = "0644"
}

resource "aws_lambda_function" "probe" {
  function_name    = "${var.name_prefix}-probe"
  description      = "Edge uptime probe: public API Gateway /ready, every minute"
  role             = aws_iam_role.probe.arn
  filename         = data.archive_file.probe.output_path
  source_code_hash = data.archive_file.probe.output_base64sha256
  handler          = "index.handler"
  runtime          = "python3.12"
  timeout          = 10
  memory_size      = 128

  environment {
    variables = {
      PROBE_URL    = "${aws_apigatewayv2_api.app.api_endpoint}/ready"
      PROBE_TARGET = "edge-ready"
    }
  }

  tags = {
    Name    = "${var.name_prefix}-probe"
    service = "reliability"
  }

  depends_on = [
    aws_iam_role_policy.probe,
    aws_cloudwatch_log_group.probe,
  ]
}

resource "aws_iam_role" "probe_scheduler" {
  name = "${var.name_prefix}-probe-scheduler"

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
    Name    = "${var.name_prefix}-probe-scheduler"
    service = "reliability"
  }
}

resource "aws_iam_role_policy" "probe_scheduler" {
  name = "${var.name_prefix}-probe-scheduler"
  role = aws_iam_role.probe_scheduler.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid      = "InvokeProbe"
      Effect   = "Allow"
      Action   = ["lambda:InvokeFunction"]
      Resource = aws_lambda_function.probe.arn
    }]
  })
}

resource "aws_scheduler_schedule" "probe" {
  name                = "${var.name_prefix}-probe"
  description         = "Edge uptime probe every minute (ADR 0010)"
  schedule_expression = "rate(1 minute)"

  flexible_time_window {
    mode = "OFF"
  }

  target {
    arn      = aws_lambda_function.probe.arn
    role_arn = aws_iam_role.probe_scheduler.arn

    retry_policy {
      maximum_retry_attempts = 0
    }
  }
}

# --- Alarms ---------------------------------------------------------------------------------

resource "aws_cloudwatch_metric_alarm" "probe_down" {
  alarm_name          = "${var.name_prefix}-probe-down"
  comparison_operator = "LessThanThreshold"
  evaluation_periods  = 2
  datapoints_to_alarm = 2
  metric_name         = "ProbeSuccess"
  namespace           = "TillFlow/Probe"
  dimensions          = { Target = "edge-ready" }
  statistic           = "Minimum"
  period              = 60
  threshold           = 1
  # No datapoint means the probe itself did not run: treat as down, not as fine.
  treat_missing_data = "breaching"
  alarm_actions      = [aws_sns_topic.alerts.arn]
  ok_actions         = [aws_sns_topic.alerts.arn]

  alarm_description = jsonencode({
    environment       = var.environment
    service           = "edge"
    symptom           = "Public /ready failing for 2 minutes (API Gateway -> ALB -> POS)"
    slo_impact        = "Every journey through the public edge; POS 99.9% budget"
    observed          = "ProbeSuccess minimum < 1"
    grafana_panel     = local.grafana_dashboard
    runbook           = "${local.runbook_url}#probe-down"
    owner             = "@emebetgirmay"
    first_safe_action = "Check service-down alarms and ECS task health first; do not redeploy blind"
  })

  tags = { service = "reliability" }
}

resource "aws_cloudwatch_metric_alarm" "service_down" {
  for_each = local.slo_services

  alarm_name          = "${var.name_prefix}-${each.key}-down"
  comparison_operator = "LessThanThreshold"
  evaluation_periods  = 2
  datapoints_to_alarm = 2
  metric_name         = "HealthyHostCount"
  namespace           = "AWS/ApplicationELB"
  dimensions = {
    LoadBalancer = aws_lb.main.arn_suffix
    TargetGroup  = each.value.target_group
  }
  statistic          = "Minimum"
  period             = 60
  threshold          = 1
  treat_missing_data = "breaching"
  alarm_actions      = [aws_sns_topic.alerts.arn]
  ok_actions         = [aws_sns_topic.alerts.arn]

  alarm_description = jsonencode({
    environment       = var.environment
    service           = each.key
    symptom           = "No healthy ${each.key} target behind the ALB for 2 minutes"
    slo_impact        = "${each.key} fully unavailable; ${each.value.budget}"
    observed          = "HealthyHostCount minimum < 1"
    grafana_panel     = local.grafana_dashboard
    runbook           = "${local.runbook_url}#service-down"
    owner             = each.value.owner
    first_safe_action = "Read the stopped task's reason in ECS; roll back to the last good digest if a release caused it"
  })

  tags = { service = each.key }
}

resource "aws_cloudwatch_metric_alarm" "burn" {
  for_each = local.burn_alarms

  alarm_name          = "${var.name_prefix}-${each.key}"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  threshold           = each.value.threshold
  # No traffic is not an outage.
  treat_missing_data = "notBreaching"
  alarm_actions      = [aws_sns_topic.alerts.arn]
  ok_actions         = [aws_sns_topic.alerts.arn]

  metric_query {
    id          = "error_rate"
    expression  = "IF(requests > 0, FILL(errors, 0) / requests, 0)"
    label       = "${each.value.service} 5xx share"
    return_data = true
  }

  metric_query {
    id = "errors"
    metric {
      metric_name = "HTTPCode_Target_5XX_Count"
      namespace   = "AWS/ApplicationELB"
      period      = each.value.period
      stat        = "Sum"
      dimensions = {
        LoadBalancer = aws_lb.main.arn_suffix
        TargetGroup  = each.value.target_group
      }
    }
  }

  metric_query {
    id = "requests"
    metric {
      metric_name = "RequestCount"
      namespace   = "AWS/ApplicationELB"
      period      = each.value.period
      stat        = "Sum"
      dimensions = {
        LoadBalancer = aws_lb.main.arn_suffix
        TargetGroup  = each.value.target_group
      }
    }
  }

  alarm_description = jsonencode({
    environment       = var.environment
    service           = each.value.service
    symptom           = "${each.value.service} ${each.value.window} burn: 5xx share above ${format("%.2f", each.value.threshold * 100)}% (${each.value.severity})"
    slo_impact        = "SLO ${format("%.1f", each.value.slo * 100)}%; ${each.value.budget}"
    observed          = "ALB target 5xx / requests over ${each.value.period / 60} min"
    grafana_panel     = local.grafana_dashboard
    runbook           = "${local.runbook_url}#${each.value.service}-${each.value.window}-burn"
    owner             = each.value.owner
    first_safe_action = each.value.service == "payments" ? "Check Payments logs for the failing route; never resend a payment or payout to clear it" : "Check POS /ready and the Payments dependency; do not replay sales by hand"
  })

  tags = { service = each.value.service }
}

resource "aws_cloudwatch_metric_alarm" "ecs_cpu_high" {
  for_each = {
    pos      = aws_ecs_service.pos.name
    payments = aws_ecs_service.payments.name
  }

  alarm_name          = "${var.name_prefix}-${each.key}-cpu-high"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 2
  datapoints_to_alarm = 2
  metric_name         = "CPUUtilization"
  namespace           = "AWS/ECS"
  dimensions = {
    ClusterName = aws_ecs_cluster.main.name
    ServiceName = each.value
  }
  statistic          = "Average"
  period             = 300
  threshold          = 70
  treat_missing_data = "notBreaching"
  alarm_actions      = [aws_sns_topic.alerts.arn]
  ok_actions         = [aws_sns_topic.alerts.arn]

  alarm_description = jsonencode({
    environment       = var.environment
    service           = each.key
    symptom           = "${each.key} CPU above 70% for 10 minutes"
    slo_impact        = "Latency and error budget at risk if it keeps climbing"
    observed          = "AWS/ECS CPUUtilization average > 70"
    grafana_panel     = local.grafana_dashboard
    runbook           = "${local.runbook_url}#ecs-cpu-high"
    owner             = "@emebetgirmay"
    first_safe_action = "Check request rate and recent releases; scale out before rolling back"
  })

  tags = { service = each.key }
}

# --- 5xx the ALB generates itself ------------------------------------------------------------
# HTTPCode_ELB_5XX_Count is what the load balancer answers when a target gives no usable
# response (a dropped connection, a timeout): the G3 k6 soak's 110 x 502 were all of this kind,
# and the per-service burn alarms above never saw them, because they count what the targets
# returned (HTTPCode_Target_5XX_Count). AWS reports ELB-generated codes per load balancer only,
# so this pair covers both services at once with the stricter SLO (POS, 99.9%).

resource "aws_cloudwatch_metric_alarm" "alb_5xx_burn" {
  for_each = local.burn_windows

  alarm_name          = "${var.name_prefix}-alb-5xx-${each.key}-burn"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  threshold           = each.value.factor * (1 - local.slo_services.pos.slo)
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alerts.arn]
  ok_actions          = [aws_sns_topic.alerts.arn]

  # RequestCount only counts requests the ALB could send to a target, so the 503s it answers when a
  # target group has no healthy target are not in it: dividing by it alone reads 0 in a total
  # outage (G4 game day, 2026-09-30: 7 errors against 5 requests in one minute, alarm stayed OK).
  metric_query {
    id          = "error_rate"
    expression  = "IF(FILL(requests, 0) + FILL(errors, 0) > 0, FILL(errors, 0) / (FILL(requests, 0) + FILL(errors, 0)), 0)"
    label       = "ALB-generated 5xx share of all requests"
    return_data = true
  }

  metric_query {
    id = "errors"
    metric {
      metric_name = "HTTPCode_ELB_5XX_Count"
      namespace   = "AWS/ApplicationELB"
      period      = each.value.period
      stat        = "Sum"
      dimensions  = { LoadBalancer = aws_lb.main.arn_suffix }
    }
  }

  metric_query {
    id = "requests"
    metric {
      metric_name = "RequestCount"
      namespace   = "AWS/ApplicationELB"
      period      = each.value.period
      stat        = "Sum"
      dimensions  = { LoadBalancer = aws_lb.main.arn_suffix }
    }
  }

  alarm_description = jsonencode({
    environment       = var.environment
    service           = "edge"
    symptom           = "ALB-generated 5xx ${each.key} burn: above ${format("%.2f", each.value.factor * (1 - local.slo_services.pos.slo) * 100)}% of requests (${each.value.severity})"
    slo_impact        = "POS and Payments availability; these errors never reach the services' own 5xx counts"
    observed          = "HTTPCode_ELB_5XX_Count / (RequestCount + HTTPCode_ELB_5XX_Count) for the whole ALB over ${each.value.period / 60} min"
    grafana_panel     = local.grafana_dashboard
    runbook           = "${local.runbook_url}#alb-5xx-${each.key}-burn"
    owner             = "@emebetgirmay"
    first_safe_action = "Find which target dropped the connection: search the service logs for Traceback in the same minutes; do not restart blind"
  })

  tags = { service = "platform" }
}
