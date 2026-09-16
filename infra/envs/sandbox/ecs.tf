resource "aws_ecs_cluster" "main" {
  name = var.name_prefix

  setting {
    name  = "containerInsights"
    value = "enabled"
  }

  tags = {
    Name    = var.name_prefix
    service = "platform"
  }
}

resource "aws_ecs_task_definition" "pos" {
  family                   = "${var.name_prefix}-pos"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = "256"
  memory                   = "512"
  execution_role_arn       = aws_iam_role.pos_exec.arn
  task_role_arn            = aws_iam_role.pos_task.arn

  container_definitions = jsonencode([
    {
      name      = "pos"
      image     = local.pos_image
      essential = true
      portMappings = [
        {
          containerPort = var.pos_container_port
          protocol      = "tcp"
        }
      ]
      environment = [
        { name = "PORT", value = tostring(var.pos_container_port) },
        { name = "OTEL_SERVICE_NAME", value = "pos" },
        { name = "OTEL_EXPORTER_OTLP_ENDPOINT", value = "http://127.0.0.1:4317" }
      ]
      healthCheck = {
        command     = ["CMD-SHELL", "python -c \"import urllib.request; urllib.request.urlopen('http://127.0.0.1:${var.pos_container_port}/health')\" || exit 1"]
        interval    = 30
        timeout     = 5
        retries     = 3
        startPeriod = 10
      }
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.pos.name
          "awslogs-region"        = var.aws_region
          "awslogs-stream-prefix" = "pos"
        }
      }
      readonlyRootFilesystem = true
      user                   = "10001:10001"
    },
    {
      name      = "adot-collector"
      image     = "public.ecr.aws/aws-observability/aws-otel-collector:v0.41.1"
      essential = false
      command   = ["--config=/etc/ecs/ecs-cloudwatch-xray.yaml"]
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.pos.name
          "awslogs-region"        = var.aws_region
          "awslogs-stream-prefix" = "adot"
        }
      }
    }
  ])

  tags = {
    Name    = "${var.name_prefix}-pos"
    service = "pos"
  }
}

resource "aws_ecs_service" "pos" {
  name            = "${var.name_prefix}-pos"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.pos.arn
  desired_count   = 1
  launch_type     = "FARGATE"

  network_configuration {
    subnets          = aws_subnet.private[*].id
    security_groups  = [aws_security_group.pos.id]
    assign_public_ip = false
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.pos.arn
    container_name   = "pos"
    container_port   = var.pos_container_port
  }

  depends_on = [aws_lb_listener.http]

  tags = {
    Name    = "${var.name_prefix}-pos"
    service = "pos"
  }
}
