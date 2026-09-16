resource "aws_security_group" "alb" {
  name        = "${var.name_prefix}-alb"
  description = "Public ALB for G1 smoke"
  vpc_id      = aws_vpc.main.id

  ingress {
    description = "HTTP smoke (G1; HTTPS/API GW later)"
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  # Only forward to tasks inside the VPC (not the public internet).
  egress {
    description = "To POS tasks in VPC"
    from_port   = var.pos_container_port
    to_port     = var.pos_container_port
    protocol    = "tcp"
    cidr_blocks = [var.vpc_cidr]
  }

  tags = {
    Name    = "${var.name_prefix}-alb"
    service = "platform"
  }
}

resource "aws_security_group" "pos" {
  name        = "${var.name_prefix}-pos"
  description = "POS ECS tasks"
  vpc_id      = aws_vpc.main.id

  ingress {
    description     = "From ALB"
    from_port       = var.pos_container_port
    to_port         = var.pos_container_port
    protocol        = "tcp"
    security_groups = [aws_security_group.alb.id]
  }

  # Fargate pulls ECR/logs/OTLP via NAT — requires HTTPS egress.
  # Accepted in .trivyignore for G1 (owner emebetgirmay, expires 2026-10-21).
  egress {
    description = "HTTPS via NAT (ECR, CloudWatch, APIs)"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Name    = "${var.name_prefix}-pos"
    service = "pos"
  }
}

resource "aws_lb" "main" {
  name                       = "${var.name_prefix}-alb"
  load_balancer_type         = "application"
  internal                   = false
  security_groups            = [aws_security_group.alb.id]
  subnets                    = aws_subnet.public[*].id
  drop_invalid_header_fields = true

  tags = {
    Name    = "${var.name_prefix}-alb"
    service = "platform"
  }
}

resource "aws_lb_target_group" "pos" {
  name        = "${var.name_prefix}-pos"
  port        = var.pos_container_port
  protocol    = "HTTP"
  vpc_id      = aws_vpc.main.id
  target_type = "ip"

  health_check {
    enabled             = true
    path                = "/health"
    protocol            = "HTTP"
    matcher             = "200"
    interval            = 30
    healthy_threshold   = 2
    unhealthy_threshold = 3
  }

  tags = {
    Name    = "${var.name_prefix}-pos"
    service = "pos"
  }
}

resource "aws_lb_listener" "http" {
  load_balancer_arn = aws_lb.main.arn
  port              = 80
  protocol          = "HTTP"

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.pos.arn
  }
}
