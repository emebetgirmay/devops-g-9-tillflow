# Public edge: API Gateway HTTP API → VPC Link → internal ALB (P0-1).

resource "aws_apigatewayv2_vpc_link" "app" {
  name               = "${var.name_prefix}-api-vpclink"
  security_group_ids = [aws_security_group.vpclink.id]
  subnet_ids         = aws_subnet.private[*].id

  lifecycle {
    create_before_destroy = true
  }

  tags = {
    Name    = "${var.name_prefix}-api-vpclink"
    service = "platform"
  }
}

resource "aws_apigatewayv2_api" "app" {
  name          = "${var.name_prefix}-api"
  protocol_type = "HTTP"
  description   = "TillFlow public HTTP API — routes to internal ALB via VPC Link."

  cors_configuration {
    allow_headers = ["content-type", "authorization", "x-amzn-trace-id", "idempotency-key"]
    allow_methods = ["GET", "POST", "PUT", "DELETE", "OPTIONS"]
    allow_origins = ["*"]
    max_age       = 300
  }

  tags = {
    Name    = "${var.name_prefix}-api"
    service = "platform"
  }
}

resource "aws_apigatewayv2_integration" "alb" {
  api_id             = aws_apigatewayv2_api.app.id
  integration_type   = "HTTP_PROXY"
  integration_uri    = aws_lb_listener.http.arn
  integration_method = "ANY"
  connection_type    = "VPC_LINK"
  connection_id      = aws_apigatewayv2_vpc_link.app.id

  timeout_milliseconds = 29000
}

resource "aws_apigatewayv2_route" "proxy" {
  api_id    = aws_apigatewayv2_api.app.id
  route_key = "ANY /{proxy+}"
  target    = "integrations/${aws_apigatewayv2_integration.alb.id}"
}

resource "aws_apigatewayv2_route" "root" {
  api_id    = aws_apigatewayv2_api.app.id
  route_key = "ANY /"
  target    = "integrations/${aws_apigatewayv2_integration.alb.id}"
}

resource "aws_cloudwatch_log_group" "apigw" {
  name              = "/${var.name_prefix}/api-gateway"
  retention_in_days = 14

  tags = {
    Name    = "/${var.name_prefix}/api-gateway"
    service = "platform"
  }
}

resource "aws_apigatewayv2_stage" "default" {
  api_id      = aws_apigatewayv2_api.app.id
  name        = "$default"
  auto_deploy = true

  default_route_settings {
    detailed_metrics_enabled = true
    throttling_burst_limit   = 200
    throttling_rate_limit    = 100
  }

  access_log_settings {
    destination_arn = aws_cloudwatch_log_group.apigw.arn
    format = jsonencode({
      requestId      = "$context.requestId"
      ip             = "$context.identity.sourceIp"
      requestTime    = "$context.requestTime"
      httpMethod     = "$context.httpMethod"
      routeKey       = "$context.routeKey"
      status         = "$context.status"
      protocol       = "$context.protocol"
      responseLength = "$context.responseLength"
      integrationErr = "$context.integrationErrorMessage"
    })
  }

  tags = {
    Name    = "${var.name_prefix}-api-default"
    service = "platform"
  }
}
