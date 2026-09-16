resource "aws_cloudwatch_log_group" "pos" {
  name              = "/${var.name_prefix}/pos"
  retention_in_days = 14

  tags = {
    Name    = "/${var.name_prefix}/pos"
    service = "pos"
  }
}

resource "aws_iam_role" "pos_exec" {
  name = "${var.name_prefix}-pos-exec"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action    = "sts:AssumeRole"
      Effect    = "Allow"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
    }]
  })

  tags = {
    Name    = "${var.name_prefix}-pos-exec"
    service = "pos"
  }
}

resource "aws_iam_role_policy_attachment" "pos_exec" {
  role       = aws_iam_role.pos_exec.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

resource "aws_iam_role" "pos_task" {
  name = "${var.name_prefix}-pos-task"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action    = "sts:AssumeRole"
      Effect    = "Allow"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
    }]
  })

  tags = {
    Name    = "${var.name_prefix}-pos-task"
    service = "pos"
  }
}

# ADOT sidecar → CloudWatch metrics / X-Ray
resource "aws_iam_role_policy" "pos_task_telemetry" {
  name = "${var.name_prefix}-pos-telemetry"
  role = aws_iam_role.pos_task.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "logs:PutLogEvents",
          "logs:CreateLogGroup",
          "logs:CreateLogStream",
          "logs:DescribeLogStreams",
          "logs:DescribeLogGroups",
          "cloudwatch:PutMetricData",
          "xray:PutTraceSegments",
          "xray:PutTelemetryRecords",
          "xray:GetSamplingRules",
          "xray:GetSamplingTargets",
          "xray:GetSamplingStatisticSummaries",
          "ssm:GetParameters"
        ]
        Resource = "*"
      }
    ]
  })
}
