# Grafana Cloud reads CloudWatch, Logs Insights and X-Ray through this role (ADR 0010 section 1).
# Only Grafana Cloud's AWS account may assume it, and only with our stack's external ID, so
# another Grafana Cloud customer cannot point their stack at it. Read-only; no access keys.

resource "aws_iam_role" "grafana_read" {
  name        = "${var.name_prefix}-grafana-read"
  description = "Grafana Cloud stack ${var.grafana_url}: read-only CloudWatch, Logs Insights, X-Ray"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "GrafanaCloudStack"
      Action    = "sts:AssumeRole"
      Effect    = "Allow"
      Principal = { AWS = "arn:aws:iam::${var.grafana_cloud_aws_account_id}:root" }
      Condition = {
        StringEquals = { "sts:ExternalId" = var.grafana_external_id }
      }
    }]
  })

  tags = {
    Name    = "${var.name_prefix}-grafana-read"
    service = "reliability"
  }
}

resource "aws_iam_role_policy" "grafana_read" {
  name = "${var.name_prefix}-grafana-read"
  role = aws_iam_role.grafana_read.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "CloudWatchRead"
        Effect = "Allow"
        Action = [
          "cloudwatch:DescribeAlarms",
          "cloudwatch:DescribeAlarmsForMetric",
          "cloudwatch:DescribeAlarmHistory",
          "cloudwatch:GetMetricData",
          "cloudwatch:GetMetricStatistics",
          "cloudwatch:ListMetrics",
          "cloudwatch:GetInsightRuleReport"
        ]
        Resource = "*"
      },
      {
        # Listing and polling queries cannot be scoped to a log group.
        Sid    = "LogsInsightsControl"
        Effect = "Allow"
        Action = [
          "logs:DescribeLogGroups",
          "logs:GetQueryResults",
          "logs:StopQuery"
        ]
        Resource = "*"
      },
      {
        # Reading log content: our own log groups only.
        Sid    = "LogsReadOwnGroups"
        Effect = "Allow"
        Action = [
          "logs:GetLogGroupFields",
          "logs:StartQuery",
          "logs:GetLogEvents",
          "logs:FilterLogEvents",
          "logs:DescribeLogStreams"
        ]
        Resource = [
          "arn:aws:logs:${var.aws_region}:${local.account_id}:log-group:/${var.name_prefix}/*",
          "arn:aws:logs:${var.aws_region}:${local.account_id}:log-group:/aws/lambda/${var.name_prefix}-*"
        ]
      },
      {
        Sid    = "XRayRead"
        Effect = "Allow"
        Action = [
          "xray:BatchGetTraces",
          "xray:GetTraceSummaries",
          "xray:GetTraceGraph",
          "xray:GetGroups",
          "xray:GetServiceGraph",
          "xray:GetTimeSeriesServiceStatistics",
          "xray:GetInsightSummaries",
          "xray:GetInsight"
        ]
        Resource = "*"
      },
      {
        # Dimension and region pickers in the query editor.
        Sid      = "Lookups"
        Effect   = "Allow"
        Action   = ["ec2:DescribeRegions", "tag:GetResources"]
        Resource = "*"
      }
    ]
  })
}
