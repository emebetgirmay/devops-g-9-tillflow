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

output "health_url" {
  value = "http://${aws_lb.main.dns_name}/health"
}

output "pos_image" {
  value = local.pos_image
}

output "ci_role_arn" {
  value = aws_iam_role.ci_deploy.arn
}

output "ready_url" {
  value = "http://${aws_lb.main.dns_name}/ready"
}

output "version_url" {
  value = "http://${aws_lb.main.dns_name}/version"
}
