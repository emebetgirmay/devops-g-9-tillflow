# RDS PostgreSQL (ADR 0002): one instance, one database `tillflow`, one schema and one login role
# per service (pos, payments, commission). Replaces the SQLite file inside each task, which a
# restart loses.
#
# Passwords never pass through Terraform, its state or CI:
#   - the master password is created and kept by RDS in Secrets Manager (manage_master_user_password);
#   - each service's secret (devops-g9/db/<service>) is created here empty, and its value is written
#     once by a person with infra/scripts/rds-bootstrap.sh, which then runs the one-off
#     devops-g9-db-bootstrap task below inside the VPC to create the roles and schemas.
#
# A service moves to RDS by flipping its variable (pos_database, payments_database) in a reviewed
# PR, once its code has a Postgres driver: the task then gets DATABASE_URL from its own secret.

variable "pos_database" {
  description = "Where POS keeps its data: sqlite (file in the task, lost on restart) or rds."
  type        = string
  default     = "sqlite"

  validation {
    condition     = contains(["sqlite", "rds"], var.pos_database)
    error_message = "pos_database must be sqlite or rds."
  }
}

variable "payments_database" {
  description = "Where Payments keeps its data: sqlite (file in the task, lost on restart) or rds."
  type        = string
  default     = "rds" # image has the driver (services/payments/requirements.txt); bootstrap has run

  validation {
    condition     = contains(["sqlite", "rds"], var.payments_database)
    error_message = "payments_database must be sqlite or rds."
  }
}

variable "db_bootstrap_image" {
  description = "psql client for the one-off bootstrap task; same minor as the server."
  type        = string
  default     = "postgres:16.15-alpine@sha256:721873c34ceb9f8d8fc265984940dc982404c105f19ad51be9fdc5970a6080ea"
}

locals {
  db_name     = "tillflow"
  db_services = toset(["pos", "payments", "commission"])

  # What each service's task receives as DATABASE_URL (keys written by rds-bootstrap.sh):
  #   sqlalchemy_url  postgresql+psycopg://...   POS (SQLAlchemy)
  #   url             postgresql://...           Payments and Commission (psycopg directly)
  pos_database_secrets = var.pos_database == "rds" ? [
    { name = "DATABASE_URL", valueFrom = "${aws_secretsmanager_secret.db["pos"].arn}:sqlalchemy_url::" },
  ] : []
  payments_database_secrets = var.payments_database == "rds" ? [
    { name = "DATABASE_URL", valueFrom = "${aws_secretsmanager_secret.db["payments"].arn}:url::" },
  ] : []
}

# --- Network ------------------------------------------------------------------------------

resource "aws_db_subnet_group" "main" {
  name       = "${var.name_prefix}-db"
  subnet_ids = aws_subnet.private[*].id

  tags = {
    Name    = "${var.name_prefix}-db"
    service = "platform"
  }
}

resource "aws_security_group" "db" {
  name        = "${var.name_prefix}-db"
  description = "RDS PostgreSQL: 5432 from the service tasks and the bootstrap task only"
  vpc_id      = aws_vpc.main.id

  ingress {
    description = "PostgreSQL from POS, Payments and the bootstrap task"
    from_port   = 5432
    to_port     = 5432
    protocol    = "tcp"
    security_groups = [
      aws_security_group.pos.id,
      aws_security_group.payments.id,
      aws_security_group.db_bootstrap.id,
    ]
  }

  tags = {
    Name    = "${var.name_prefix}-db"
    service = "platform"
  }
}

# --- Instance -----------------------------------------------------------------------------

resource "aws_db_parameter_group" "main" {
  name   = "${var.name_prefix}-pg16"
  family = "postgres16"

  parameter {
    name  = "rds.force_ssl"
    value = "1"
  }

  # Slow statements show in the RDS log; the services' own latency metrics stay the SLI.
  parameter {
    name  = "log_min_duration_statement"
    value = "500"
  }

  tags = {
    Name    = "${var.name_prefix}-pg16"
    service = "platform"
  }
}

# RDS writes the exported PostgreSQL log here; created first so it has a retention (RDS would
# otherwise create it with none).
resource "aws_cloudwatch_log_group" "db" {
  name              = "/aws/rds/instance/${var.name_prefix}-db/postgresql"
  retention_in_days = 14

  tags = {
    Name    = "/aws/rds/instance/${var.name_prefix}-db/postgresql"
    service = "platform"
  }
}

resource "aws_db_instance" "main" {
  identifier     = "${var.name_prefix}-db"
  engine         = "postgres"
  engine_version = "16.15"
  instance_class = "db.t4g.micro"
  db_name        = local.db_name

  username                    = "tillflow_admin"
  manage_master_user_password = true

  allocated_storage     = 20
  max_allocated_storage = 50
  storage_type          = "gp3"
  storage_encrypted     = true

  db_subnet_group_name   = aws_db_subnet_group.main.name
  vpc_security_group_ids = [aws_security_group.db.id]
  parameter_group_name   = aws_db_parameter_group.main.name
  publicly_accessible    = false
  multi_az               = false # ADR 0002: single-AZ for the sandbox

  # RPO: point-in-time restore to within about 5 minutes, for 7 days. Backups at 23:00 EAT,
  # after trading and before Commission closes the day.
  backup_retention_period = 7
  backup_window           = "20:00-20:30"
  maintenance_window      = "sun:21:00-sun:22:00"
  copy_tags_to_snapshot   = true

  # The minor version is pinned here and upgraded by PR, not by AWS in the maintenance window.
  auto_minor_version_upgrade = false

  performance_insights_enabled          = true
  performance_insights_retention_period = 7
  enabled_cloudwatch_logs_exports       = ["postgresql"]
  depends_on                            = [aws_cloudwatch_log_group.db]

  # Destroy and rebuild (G5) must be possible without a console step; the final snapshot keeps
  # the data when the instance is destroyed on purpose.
  deletion_protection       = false
  skip_final_snapshot       = false
  final_snapshot_identifier = "${var.name_prefix}-db-final"

  tags = {
    Name    = "${var.name_prefix}-db"
    service = "platform"
  }
}

# --- One secret per service (value written by infra/scripts/rds-bootstrap.sh) --------------

resource "aws_secretsmanager_secret" "db" {
  for_each = local.db_services

  name                    = "${var.name_prefix}/db/${each.key}"
  description             = "${each.key} login for ${local.db_name} on ${var.name_prefix}-db. Value written by infra/scripts/rds-bootstrap.sh, never by Terraform."
  recovery_window_in_days = 0 # destroy and rebuild can recreate the same name at once

  tags = {
    Name    = "${var.name_prefix}/db/${each.key}"
    service = each.key
  }
}

resource "aws_iam_role_policy" "pos_exec_db" {
  count = var.pos_database == "rds" ? 1 : 0
  name  = "${var.name_prefix}-pos-db-secret"
  role  = aws_iam_role.pos_exec.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["secretsmanager:GetSecretValue"]
      Resource = [aws_secretsmanager_secret.db["pos"].arn]
    }]
  })
}

resource "aws_iam_role_policy" "payments_exec_db" {
  count = var.payments_database == "rds" ? 1 : 0
  name  = "${var.name_prefix}-payments-db-secret"
  role  = aws_iam_role.payments_exec.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["secretsmanager:GetSecretValue"]
      Resource = [aws_secretsmanager_secret.db["payments"].arn]
    }]
  })
}

# --- One-off bootstrap task: roles, schemas, grants -----------------------------------------
# Idempotent: re-running it resets each role's password to the one in its secret and changes
# nothing else. Started only by infra/scripts/rds-bootstrap.sh, never a service.

locals {
  db_bootstrap_sql = <<-SQL
    \set ON_ERROR_STOP on
    REVOKE ALL ON DATABASE ${local.db_name} FROM PUBLIC;
    REVOKE CREATE ON SCHEMA public FROM PUBLIC;
    SELECT format('CREATE ROLE %I LOGIN', r) FROM unnest(ARRAY['pos', 'payments', 'commission']) AS r
      WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r) \gexec
    ALTER ROLE pos PASSWORD :'pos_pw';
    ALTER ROLE payments PASSWORD :'payments_pw';
    ALTER ROLE commission PASSWORD :'commission_pw';
    GRANT pos, payments, commission TO CURRENT_USER;
    CREATE SCHEMA IF NOT EXISTS pos AUTHORIZATION pos;
    CREATE SCHEMA IF NOT EXISTS payments AUTHORIZATION payments;
    CREATE SCHEMA IF NOT EXISTS commission AUTHORIZATION commission;
    GRANT CONNECT ON DATABASE ${local.db_name} TO pos, payments, commission;
    ALTER ROLE pos SET search_path = pos;
    ALTER ROLE payments SET search_path = payments;
    ALTER ROLE commission SET search_path = commission;
    SELECT r.rolname AS role, n.nspname AS schema
      FROM pg_namespace n JOIN pg_roles r ON r.oid = n.nspowner
      WHERE n.nspname IN ('pos', 'payments', 'commission') ORDER BY 1;
  SQL
}

resource "aws_cloudwatch_log_group" "db_bootstrap" {
  name              = "/${var.name_prefix}/db-bootstrap"
  retention_in_days = 14

  tags = {
    Name    = "/${var.name_prefix}/db-bootstrap"
    service = "platform"
  }
}

resource "aws_security_group" "db_bootstrap" {
  name        = "${var.name_prefix}-db-bootstrap"
  description = "One-off DB bootstrap task: PostgreSQL in the VPC, HTTPS via NAT to pull its image"
  vpc_id      = aws_vpc.main.id

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
    Name    = "${var.name_prefix}-db-bootstrap"
    service = "platform"
  }
}

resource "aws_iam_role" "db_bootstrap_exec" {
  name = "${var.name_prefix}-db-bootstrap-exec"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action    = "sts:AssumeRole"
      Effect    = "Allow"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
    }]
  })

  tags = {
    Name    = "${var.name_prefix}-db-bootstrap-exec"
    service = "platform"
  }
}

resource "aws_iam_role_policy_attachment" "db_bootstrap_exec" {
  role       = aws_iam_role.db_bootstrap_exec.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

resource "aws_iam_role_policy" "db_bootstrap_secrets" {
  name = "${var.name_prefix}-db-bootstrap-secrets"
  role = aws_iam_role.db_bootstrap_exec.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = ["secretsmanager:GetSecretValue"]
      Resource = concat(
        [aws_db_instance.main.master_user_secret[0].secret_arn],
        [for s in aws_secretsmanager_secret.db : s.arn],
      )
    }]
  })
}

resource "aws_ecs_task_definition" "db_bootstrap" {
  family                   = "${var.name_prefix}-db-bootstrap"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = "256"
  memory                   = "512"
  execution_role_arn       = aws_iam_role.db_bootstrap_exec.arn

  container_definitions = jsonencode([
    {
      name                   = "psql"
      image                  = var.db_bootstrap_image
      essential              = true
      readonlyRootFilesystem = true
      user                   = "70:70" # postgres user in the alpine image
      entryPoint             = ["sh", "-c"]
      command = [join(" ", [
        "printf '%s' \"$BOOTSTRAP_SQL\" | psql -X",
        "-v pos_pw=\"$POS_PASSWORD\" -v payments_pw=\"$PAYMENTS_PASSWORD\" -v commission_pw=\"$COMMISSION_PASSWORD\"",
      ])]
      environment = [
        { name = "PGHOST", value = aws_db_instance.main.address },
        { name = "PGPORT", value = tostring(aws_db_instance.main.port) },
        { name = "PGDATABASE", value = local.db_name },
        { name = "PGSSLMODE", value = "require" },
        { name = "BOOTSTRAP_SQL", value = local.db_bootstrap_sql },
      ]
      secrets = [
        { name = "PGUSER", valueFrom = "${aws_db_instance.main.master_user_secret[0].secret_arn}:username::" },
        { name = "PGPASSWORD", valueFrom = "${aws_db_instance.main.master_user_secret[0].secret_arn}:password::" },
        { name = "POS_PASSWORD", valueFrom = "${aws_secretsmanager_secret.db["pos"].arn}:password::" },
        { name = "PAYMENTS_PASSWORD", valueFrom = "${aws_secretsmanager_secret.db["payments"].arn}:password::" },
        { name = "COMMISSION_PASSWORD", valueFrom = "${aws_secretsmanager_secret.db["commission"].arn}:password::" },
      ]
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.db_bootstrap.name
          "awslogs-region"        = var.aws_region
          "awslogs-stream-prefix" = "bootstrap"
        }
      }
    }
  ])

  tags = {
    Name    = "${var.name_prefix}-db-bootstrap"
    service = "platform"
  }
}

# --- Alarms -------------------------------------------------------------------------------

locals {
  db_alarms = {
    cpu-high = {
      metric     = "CPUUtilization"
      comparison = "GreaterThanThreshold"
      threshold  = 80
      symptom    = "RDS CPU above 80% for 10 minutes"
      observed   = "AWS/RDS CPUUtilization average > 80"
      action     = "Check Performance Insights for the top statement and which service sends it; do not resize during Commission's run (00:00-06:30 EAT)"
    }
    storage-low = {
      metric     = "FreeStorageSpace"
      comparison = "LessThanThreshold"
      threshold  = 2 * 1024 * 1024 * 1024
      symptom    = "RDS free storage below 2 GiB (autoscaling caps at 50 GiB)"
      observed   = "AWS/RDS FreeStorageSpace average < 2 GiB"
      action     = "Find the growing table per schema; raise max_allocated_storage by PR before it fills"
    }
    memory-low = {
      metric     = "FreeableMemory"
      comparison = "LessThanThreshold"
      threshold  = 100 * 1024 * 1024
      symptom    = "RDS freeable memory below 100 MiB for 10 minutes (db.t4g.micro has 1 GiB)"
      observed   = "AWS/RDS FreeableMemory average < 100 MiB"
      action     = "Check connection counts per service (pool sizes); resize the instance class by PR if it persists"
    }
  }
}

resource "aws_cloudwatch_metric_alarm" "db" {
  for_each = local.db_alarms

  alarm_name          = "${var.name_prefix}-db-${each.key}"
  comparison_operator = each.value.comparison
  evaluation_periods  = 2
  datapoints_to_alarm = 2
  metric_name         = each.value.metric
  namespace           = "AWS/RDS"
  dimensions          = { DBInstanceIdentifier = aws_db_instance.main.identifier }
  statistic           = "Average"
  period              = 300
  threshold           = each.value.threshold
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alerts.arn]
  ok_actions          = [aws_sns_topic.alerts.arn]

  alarm_description = jsonencode({
    environment       = var.environment
    service           = "database"
    symptom           = each.value.symptom
    slo_impact        = "Every service on RDS: POS, Payments and Commission"
    observed          = each.value.observed
    grafana_panel     = local.grafana_dashboard
    runbook           = "${local.runbook_url}#db-${each.key}"
    owner             = "@emebetgirmay"
    first_safe_action = each.value.action
  })

  tags = { service = "platform" }
}

# --- Outputs ------------------------------------------------------------------------------

output "db_endpoint" {
  value = aws_db_instance.main.address
}

output "db_master_secret_arn" {
  value = aws_db_instance.main.master_user_secret[0].secret_arn
}

output "db_bootstrap_task_definition_arn" {
  value = aws_ecs_task_definition.db_bootstrap.arn
}

output "db_bootstrap_security_group_id" {
  value = aws_security_group.db_bootstrap.id
}
