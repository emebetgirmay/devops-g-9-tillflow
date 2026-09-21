resource "aws_cloudwatch_log_group" "pos" {
  name              = "/${var.name_prefix}/pos"
  retention_in_days = 14

  tags = {
    Name    = "/${var.name_prefix}/pos"
    service = "pos"
  }
}

resource "aws_cloudwatch_log_group" "adot_pos" {
  name              = "/${var.name_prefix}/adot/pos"
  retention_in_days = 14

  tags = {
    Name    = "/${var.name_prefix}/adot/pos"
    service = "pos"
  }
}

resource "aws_cloudwatch_log_group" "payments" {
  name              = "/${var.name_prefix}/payments"
  retention_in_days = 14

  tags = {
    Name    = "/${var.name_prefix}/payments"
    service = "payments"
  }
}

resource "aws_cloudwatch_log_group" "adot_payments" {
  name              = "/${var.name_prefix}/adot/payments"
  retention_in_days = 14

  tags = {
    Name    = "/${var.name_prefix}/adot/payments"
    service = "payments"
  }
}

resource "aws_cloudwatch_log_group" "adot_metrics" {
  name              = "/${var.name_prefix}/adot/metrics"
  retention_in_days = 14

  tags = {
    Name    = "/${var.name_prefix}/adot/metrics"
    service = "platform"
  }
}

# Minimal ADOT config: OTLP receivers + health_check extension (P0-2).
resource "aws_ssm_parameter" "adot_config" {
  name        = "/${var.name_prefix}/adot/config"
  description = "ADOT collector config for ECS sidecars"
  type        = "String"
  tier        = "Standard"

  value = <<-EOT
    receivers:
      otlp:
        protocols:
          grpc:
            endpoint: 0.0.0.0:4317
          http:
            endpoint: 0.0.0.0:4318
    processors:
      batch:
        timeout: 5s
    exporters:
      awsxray:
        region: ${var.aws_region}
      awsemf:
        region: ${var.aws_region}
        namespace: TillFlow
        log_group_name: /${var.name_prefix}/adot/metrics
    extensions:
      health_check:
        endpoint: 0.0.0.0:13133
        path: /
    service:
      extensions: [health_check]
      pipelines:
        traces:
          receivers: [otlp]
          processors: [batch]
          exporters: [awsxray]
        metrics:
          receivers: [otlp]
          processors: [batch]
          exporters: [awsemf]
  EOT

  tags = {
    Name    = "/${var.name_prefix}/adot/config"
    service = "platform"
  }
}

locals {
  # Self-healthy placeholder so first apply does not crash-loop on missing :bootstrap (P0-3).
  pos_placeholder_image = "public.ecr.aws/docker/library/busybox:1.37.0"
  pos_uses_placeholder  = var.pos_image_digest == null || var.pos_image_digest == ""
  pos_image             = local.pos_uses_placeholder ? local.pos_placeholder_image : var.pos_image_digest
  pos_placeholder_command = [
    "sh", "-c",
    "mkdir -p /tmp/www && printf '%s' '{\"status\":\"ok\",\"service\":\"pos\"}' > /tmp/www/health && printf '%s' '{\"status\":\"ready\",\"service\":\"pos\"}' > /tmp/www/ready && printf '%s' '{\"service\":\"pos\",\"commit\":\"placeholder\"}' > /tmp/www/version && exec httpd -f -p ${var.pos_container_port} -h /tmp/www",
  ]

  payments_placeholder_image = local.pos_placeholder_image
  payments_uses_placeholder  = var.payments_image_digest == null || var.payments_image_digest == ""
  payments_image             = local.payments_uses_placeholder ? local.payments_placeholder_image : var.payments_image_digest
  payments_placeholder_command = [
    "sh", "-c",
    "mkdir -p /tmp/www && printf '%s' '{\"status\":\"ok\",\"service\":\"payments\"}' > /tmp/www/health && printf '%s' '{\"status\":\"ready\",\"service\":\"payments\"}' > /tmp/www/ready && printf '%s' '{\"service\":\"payments\",\"commit\":\"placeholder\"}' > /tmp/www/version && exec httpd -f -p ${var.payments_container_port} -h /tmp/www",
  ]

  # POS → Payments over the same internal ALB (path rules forward /payments*).
  payments_base_url = "http://${aws_lb.main.dns_name}"
}

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

  volume {
    name = "tmp"
  }

  container_definitions = jsonencode(concat(
    [
      merge(
        {
          name      = "pos"
          image     = local.pos_image
          essential = true
          # SQLite under /app/data needs a writable root; Fargate /tmp volumes are root-owned.
          readonlyRootFilesystem = false
          user                   = local.pos_uses_placeholder ? "0:0" : "10001:10001"
          portMappings = [
            {
              containerPort = var.pos_container_port
              protocol      = "tcp"
            }
          ]
          environment = [
            { name = "PORT", value = tostring(var.pos_container_port) },
            { name = "DATABASE_URL", value = "sqlite:////app/data/pos.db" },
            { name = "PAYMENTS_BASE_URL", value = local.payments_base_url },
            { name = "OTEL_SERVICE_NAME", value = "pos" },
            { name = "OTEL_EXPORTER_OTLP_ENDPOINT", value = "http://127.0.0.1:4318" },
            { name = "ADOT_HEALTH_URL", value = "http://127.0.0.1:13133/" },
          ]
          mountPoints = [
            { sourceVolume = "tmp", containerPath = "/tmp", readOnly = false },
          ]
          dependsOn = [
            { containerName = "adot", condition = "HEALTHY" },
          ]
          healthCheck = {
            command     = local.pos_uses_placeholder ? ["CMD-SHELL", "wget -qO- http://127.0.0.1:${var.pos_container_port}/ready || exit 1"] : ["CMD-SHELL", "python -c \"import urllib.request; urllib.request.urlopen('http://127.0.0.1:${var.pos_container_port}/ready')\" || exit 1"]
            interval    = 15
            timeout     = 5
            retries     = 3
            startPeriod = 40
          }
          logConfiguration = {
            logDriver = "awslogs"
            options = {
              "awslogs-group"         = aws_cloudwatch_log_group.pos.name
              "awslogs-region"        = var.aws_region
              "awslogs-stream-prefix" = "pos"
            }
          }
          linuxParameters = { initProcessEnabled = true }
        },
        local.pos_uses_placeholder ? { command = local.pos_placeholder_command } : {},
      ),
      {
        name                   = "adot"
        image                  = var.adot_collector_image
        essential              = true
        readonlyRootFilesystem = true
        command                = ["--config=env:AOT_CONFIG_CONTENT"]
        environment = [
          { name = "AOT_CONFIG_CONTENT", value = aws_ssm_parameter.adot_config.value },
        ]
        mountPoints = [
          { sourceVolume = "tmp", containerPath = "/tmp", readOnly = false },
        ]
        # Image is FROM scratch - exec-form /healthcheck only (no shell).
        healthCheck = {
          command     = ["CMD", "/healthcheck"]
          interval    = 10
          timeout     = 5
          retries     = 5
          startPeriod = 30
        }
        logConfiguration = {
          logDriver = "awslogs"
          options = {
            "awslogs-group"         = aws_cloudwatch_log_group.adot_pos.name
            "awslogs-region"        = var.aws_region
            "awslogs-stream-prefix" = "adot"
          }
        }
        linuxParameters = { initProcessEnabled = true }
      },
    ]
  ))

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

  # Pipeline registers digest task defs; Terraform must not roll them back (P0-3).
  lifecycle {
    ignore_changes = [task_definition]
  }

  tags = {
    Name    = "${var.name_prefix}-pos"
    service = "pos"
  }
}

resource "aws_ecs_task_definition" "payments" {
  family                   = "${var.name_prefix}-payments"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = "256"
  memory                   = "512"
  execution_role_arn       = aws_iam_role.payments_exec.arn
  task_role_arn            = aws_iam_role.payments_task.arn

  volume {
    name = "tmp"
  }

  container_definitions = jsonencode(concat(
    [
      merge(
        {
          name      = "payments"
          image     = local.payments_image
          essential = true
          # SQLite needs a writable path; Fargate empty volumes are root-owned, so
          # keep the root FS writable and use /app/data from the image (uid 10001).
          readonlyRootFilesystem = false
          user                   = local.payments_uses_placeholder ? "0:0" : "10001:10001"
          portMappings = [
            {
              containerPort = var.payments_container_port
              protocol      = "tcp"
            }
          ]
          environment = [
            { name = "PORT", value = tostring(var.payments_container_port) },
            { name = "DATABASE_URL", value = "sqlite:////app/data/payments.db" },
            { name = "MPESA_ADAPTER", value = "fake" },
            { name = "OTEL_SERVICE_NAME", value = "payments" },
            { name = "OTEL_EXPORTER_OTLP_ENDPOINT", value = "http://127.0.0.1:4318" },
            { name = "ADOT_HEALTH_URL", value = "http://127.0.0.1:13133/" },
          ]
          mountPoints = [
            { sourceVolume = "tmp", containerPath = "/tmp", readOnly = false },
          ]
          dependsOn = [
            { containerName = "adot", condition = "HEALTHY" },
          ]
          healthCheck = {
            command     = local.payments_uses_placeholder ? ["CMD-SHELL", "wget -qO- http://127.0.0.1:${var.payments_container_port}/ready || exit 1"] : ["CMD-SHELL", "python -c \"import urllib.request; urllib.request.urlopen('http://127.0.0.1:${var.payments_container_port}/ready')\" || exit 1"]
            interval    = 15
            timeout     = 5
            retries     = 3
            startPeriod = 20
          }
          logConfiguration = {
            logDriver = "awslogs"
            options = {
              "awslogs-group"         = aws_cloudwatch_log_group.payments.name
              "awslogs-region"        = var.aws_region
              "awslogs-stream-prefix" = "payments"
            }
          }
          linuxParameters = { initProcessEnabled = true }
        },
        local.payments_uses_placeholder ? { command = local.payments_placeholder_command } : {},
      ),
      {
        name                   = "adot"
        image                  = var.adot_collector_image
        essential              = true
        readonlyRootFilesystem = true
        command                = ["--config=env:AOT_CONFIG_CONTENT"]
        environment = [
          { name = "AOT_CONFIG_CONTENT", value = aws_ssm_parameter.adot_config.value },
        ]
        mountPoints = [
          { sourceVolume = "tmp", containerPath = "/tmp", readOnly = false },
        ]
        healthCheck = {
          command     = ["CMD", "/healthcheck"]
          interval    = 10
          timeout     = 5
          retries     = 5
          startPeriod = 30
        }
        logConfiguration = {
          logDriver = "awslogs"
          options = {
            "awslogs-group"         = aws_cloudwatch_log_group.adot_payments.name
            "awslogs-region"        = var.aws_region
            "awslogs-stream-prefix" = "adot"
          }
        }
        linuxParameters = { initProcessEnabled = true }
      },
    ]
  ))

  tags = {
    Name    = "${var.name_prefix}-payments"
    service = "payments"
  }
}

resource "aws_ecs_service" "payments" {
  name            = "${var.name_prefix}-payments"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.payments.arn
  desired_count   = 1
  launch_type     = "FARGATE"

  network_configuration {
    subnets          = aws_subnet.private[*].id
    security_groups  = [aws_security_group.payments.id]
    assign_public_ip = false
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.payments.arn
    container_name   = "payments"
    container_port   = var.payments_container_port
  }

  depends_on = [aws_lb_listener_rule.payments]

  lifecycle {
    ignore_changes = [task_definition]
  }

  tags = {
    Name    = "${var.name_prefix}-payments"
    service = "payments"
  }
}
