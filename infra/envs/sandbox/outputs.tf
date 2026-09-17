output "vpc_id" {
  value = aws_vpc.main.id
}

output "public_subnet_ids" {
  value = aws_subnet.public[*].id
}

output "private_subnet_ids" {
  value = aws_subnet.private[*].id
}

output "availability_zones" {
  value = local.azs
}

output "ecr_pos_url" {
  value = aws_ecr_repository.pos.repository_url
}

output "ecs_cluster_name" {
  value = aws_ecs_cluster.main.name
}

output "ecs_service_name" {
  value = aws_ecs_service.pos.name
}

output "alb_dns_name" {
  value = aws_lb.main.dns_name
}

output "alb_internal" {
  value = aws_lb.main.internal
}

output "api_gateway_url" {
  value = aws_apigatewayv2_api.app.api_endpoint
}

output "health_url" {
  value = "${aws_apigatewayv2_api.app.api_endpoint}/health"
}

output "ready_url" {
  value = "${aws_apigatewayv2_api.app.api_endpoint}/ready"
}

output "version_url" {
  value = "${aws_apigatewayv2_api.app.api_endpoint}/version"
}

output "ci_role_arn" {
  value = aws_iam_role.ci_deploy.arn
}

output "pos_image" {
  value = local.pos_image
}
