# k6 load test inside the VPC (ADR 0010 section 5, R-8). A task definition only, never a service:
# it runs when someone starts it (evidence/reliability-ops/run-k6.sh) and exits when k6 is done.
#
# It drives Payments' capacity script (services/payments/k6/capacity.js, the FakeAdapter only)
# against the internal ALB, which routes /payments*, /payouts*, /_fake* and /_admin* to Payments.
# Running from inside the VPC is what lets /_fake and /_admin leave the public route later
# (ADR 0009 G3-7). The script travels in the task definition, so the run is exactly the reviewed
# file in this commit. The end-of-test summary is printed between markers for run-k6.sh to keep.

locals {
  k6_script = file("${path.module}/../../../services/payments/k6/capacity.js")
}

resource "aws_cloudwatch_log_group" "k6" {
  name              = "/${var.name_prefix}/k6"
  retention_in_days = 14

  tags = {
    Name    = "/${var.name_prefix}/k6"
    service = "reliability"
  }
}

resource "aws_security_group" "k6" {
  name        = "${var.name_prefix}-k6"
  description = "k6 load test task: HTTP to the internal ALB, HTTPS via NAT to pull its image"
  vpc_id      = aws_vpc.main.id

  egress {
    description = "HTTP to internal ALB"
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = [var.vpc_cidr]
  }

  # Accepted in .trivyignore (AVD-AWS-0104), same as the service tasks.
  egress {
    description = "HTTPS via NAT (image pull, CloudWatch Logs)"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Name    = "${var.name_prefix}-k6"
    service = "reliability"
  }
}

resource "aws_iam_role" "k6_exec" {
  name = "${var.name_prefix}-k6-exec"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action    = "sts:AssumeRole"
      Effect    = "Allow"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
    }]
  })

  tags = {
    Name    = "${var.name_prefix}-k6-exec"
    service = "reliability"
  }
}

resource "aws_iam_role_policy_attachment" "k6_exec" {
  role       = aws_iam_role.k6_exec.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

resource "aws_ecs_task_definition" "k6" {
  family                   = "${var.name_prefix}-k6"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = "512"
  memory                   = "1024"
  execution_role_arn       = aws_iam_role.k6_exec.arn

  volume {
    name = "tmp"
  }

  container_definitions = jsonencode([
    {
      name                   = "k6"
      image                  = var.k6_image
      essential              = true
      readonlyRootFilesystem = true
      entryPoint             = ["sh", "-c"]
      command = [join(" ", [
        "printf '%s' \"$K6_SCRIPT\" > /tmp/capacity.js &&",
        "k6 run --no-color --summary-export=/tmp/summary.json",
        "-e PAYMENTS_URL=\"$PAYMENTS_URL\" -e SOAK_DURATION=\"$SOAK_DURATION\"",
        "-e DRIVER_DURATION=\"$DRIVER_DURATION\" -e ADVANCE_SECONDS=\"$ADVANCE_SECONDS\"",
        "/tmp/capacity.js; rc=$?;",
        "echo K6_SUMMARY_BEGIN; cat /tmp/summary.json; echo; echo K6_SUMMARY_END;",
        "echo K6_EXIT_CODE=$rc; exit $rc",
      ])]
      environment = [
        { name = "K6_SCRIPT", value = local.k6_script },
        { name = "PAYMENTS_URL", value = "http://${aws_lb.main.dns_name}" },
        { name = "SOAK_DURATION", value = var.k6_soak_duration },
        { name = "DRIVER_DURATION", value = var.k6_driver_duration },
        { name = "ADVANCE_SECONDS", value = "5" },
      ]
      mountPoints = [
        { sourceVolume = "tmp", containerPath = "/tmp", readOnly = false },
      ]
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.k6.name
          "awslogs-region"        = var.aws_region
          "awslogs-stream-prefix" = "k6"
        }
      }
    }
  ])

  tags = {
    Name    = "${var.name_prefix}-k6"
    service = "reliability"
  }
}

output "k6_task_definition_arn" {
  value = aws_ecs_task_definition.k6.arn
}

output "k6_security_group_id" {
  value = aws_security_group.k6.id
}
