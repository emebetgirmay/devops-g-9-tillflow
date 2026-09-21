# Internal ALB + security groups (public edge is API Gateway).

resource "aws_security_group" "vpclink" {
  name        = "${var.name_prefix}-vpclink"
  description = "ENIs for API Gateway VPC Link"
  vpc_id      = aws_vpc.main.id

  egress {
    description = "To internal ALB"
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = [var.vpc_cidr]
  }

  tags = {
    Name    = "${var.name_prefix}-vpclink"
    service = "platform"
  }
}

resource "aws_security_group" "alb" {
  name        = "${var.name_prefix}-alb"
  description = "Internal ALB - only from VPC Link"
  vpc_id      = aws_vpc.main.id

  ingress {
    description     = "HTTP from VPC Link"
    from_port       = 80
    to_port         = 80
    protocol        = "tcp"
    security_groups = [aws_security_group.vpclink.id]
  }

  egress {
    description = "To ECS tasks in VPC (POS and Payments)"
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

  # Fargate pulls ECR/logs via NAT - requires HTTPS egress.
  # Accepted in .trivyignore (owner emebetgirmay, expires 2026-10-21).
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

resource "aws_security_group" "payments" {
  name        = "${var.name_prefix}-payments"
  description = "Payments ECS tasks"
  vpc_id      = aws_vpc.main.id

  ingress {
    description     = "From ALB"
    from_port       = var.payments_container_port
    to_port         = var.payments_container_port
    protocol        = "tcp"
    security_groups = [aws_security_group.alb.id]
  }

  # Fargate pulls ECR/logs via NAT - requires HTTPS egress.
  # Accepted in .trivyignore (owner emebetgirmay, expires 2026-10-21).
  egress {
    description = "HTTPS via NAT (ECR, CloudWatch, APIs)"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Name    = "${var.name_prefix}-payments"
    service = "payments"
  }
}

resource "aws_lb" "main" {
  name                       = "${var.name_prefix}-alb"
  load_balancer_type         = "application"
  internal                   = true
  security_groups            = [aws_security_group.alb.id]
  subnets                    = aws_subnet.private[*].id
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
    path                = "/ready"
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

resource "aws_lb_target_group" "payments" {
  name        = "${var.name_prefix}-payments"
  port        = var.payments_container_port
  protocol    = "HTTP"
  vpc_id      = aws_vpc.main.id
  target_type = "ip"

  health_check {
    enabled             = true
    path                = "/ready"
    protocol            = "HTTP"
    matcher             = "200"
    interval            = 30
    healthy_threshold   = 2
    unhealthy_threshold = 3
  }

  tags = {
    Name    = "${var.name_prefix}-payments"
    service = "payments"
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

# Payments paths on the same internal ALB (default stays POS).
resource "aws_lb_listener_rule" "payments" {
  listener_arn = aws_lb_listener.http.arn
  priority     = 10

  action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.payments.arn
  }

  condition {
    path_pattern {
      values = ["/payments*", "/payouts*", "/_fake*", "/_admin*"]
    }
  }
}
