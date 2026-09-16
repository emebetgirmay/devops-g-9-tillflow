# GitHub OIDC — use the account's existing provider (shared lab).
# Creating oidc-provider is often denied for cohort roles; G10 pattern = data source.
#
# After apply, set GitHub repo Actions variables:
#   AWS_CI_ROLE_ARN = (terraform output ci_role_arn)
#   TF_STATE_BUCKET = devops-g9-tfstate-240462142849

data "aws_iam_openid_connect_provider" "github" {
  url = "https://token.actions.githubusercontent.com"
}

resource "aws_iam_role" "ci_deploy" {
  name = "${var.name_prefix}-ci-deploy"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Principal = {
          Federated = data.aws_iam_openid_connect_provider.github.arn
        }
        Action = "sts:AssumeRoleWithWebIdentity"
        Condition = {
          StringEquals = {
            "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com"
          }
          StringLike = {
            "token.actions.githubusercontent.com:sub" = [
              "repo:${var.github_org}/${var.github_repo}:ref:refs/heads/main",
              "repo:${var.github_org}/${var.github_repo}:ref:refs/heads/platform/*",
              "repo:${var.github_org}/${var.github_repo}:pull_request",
              "repo:${var.github_org}/${var.github_repo}:environment:sandbox"
            ]
          }
        }
      }
    ]
  })

  tags = {
    Name    = "${var.name_prefix}-ci-deploy"
    service = "platform"
  }
}

# Lab-scoped: enough for terraform plan/apply + ECR push + ECS update.
resource "aws_iam_role_policy" "ci_deploy" {
  name = "${var.name_prefix}-ci-deploy"
  role = aws_iam_role.ci_deploy.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "TerraformState"
        Effect = "Allow"
        Action = [
          "s3:ListBucket",
          "s3:GetObject",
          "s3:PutObject",
          "s3:DeleteObject"
        ]
        Resource = [
          "arn:aws:s3:::devops-g9-tfstate-${local.account_id}",
          "arn:aws:s3:::devops-g9-tfstate-${local.account_id}/*"
        ]
      },
      {
        Sid    = "TerraformLock"
        Effect = "Allow"
        Action = [
          "dynamodb:DescribeTable",
          "dynamodb:GetItem",
          "dynamodb:PutItem",
          "dynamodb:DeleteItem",
          "dynamodb:UpdateItem"
        ]
        Resource = "arn:aws:dynamodb:${var.aws_region}:${local.account_id}:table/devops-g9-tflock"
      },
      {
        Sid    = "PlatformProvision"
        Effect = "Allow"
        Action = [
          "ec2:*",
          "ecs:*",
          "ecr:*",
          "elasticloadbalancing:*",
          "logs:*",
          "iam:GetRole",
          "iam:GetRolePolicy",
          "iam:ListRolePolicies",
          "iam:ListAttachedRolePolicies",
          "iam:PassRole",
          "ssm:*",
          "xray:*",
          "cloudwatch:*",
          "application-autoscaling:*",
          "servicediscovery:*"
        ]
        Resource = "*"
      },
      {
        Sid    = "ManageOwnCiRole"
        Effect = "Allow"
        Action = [
          "iam:CreateRole",
          "iam:DeleteRole",
          "iam:PutRolePolicy",
          "iam:DeleteRolePolicy",
          "iam:AttachRolePolicy",
          "iam:DetachRolePolicy",
          "iam:UpdateAssumeRolePolicy",
          "iam:TagRole",
          "iam:UntagRole",
          "iam:ListInstanceProfilesForRole"
        ]
        Resource = [
          "arn:aws:iam::${local.account_id}:role/${var.name_prefix}-*",
          "arn:aws:iam::${local.account_id}:policy/${var.name_prefix}-*"
        ]
      }
    ]
  })
}
