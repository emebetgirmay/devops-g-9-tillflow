resource "aws_ecr_repository" "pos" {
  name                 = "${var.name_prefix}/pos"
  image_tag_mutability = "IMMUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }

  encryption_configuration {
    encryption_type = "AES256"
  }

  tags = {
    Name    = "${var.name_prefix}/pos"
    service = "pos"
  }
}

resource "aws_ecr_lifecycle_policy" "pos" {
  repository = aws_ecr_repository.pos.name

  policy = jsonencode({
    rules = [
      {
        rulePriority = 1
        description  = "Keep last 10 images"
        selection = {
          tagStatus   = "any"
          countType   = "imageCountMoreThan"
          countNumber = 10
        }
        action = {
          type = "expire"
        }
      }
    ]
  })
}

locals {
  pos_image = "${aws_ecr_repository.pos.repository_url}:${var.pos_image_tag}"
}
