# GitHub OIDC — same lab constraints as devops-g10 (approved G1).
# - Cannot create the account OIDC provider (data source only).
# - Trust cannot use sub "...:*" (AWS rejects "not scoped").
# - GitHub immutable subjects use owner@id/repo@id — match both forms.
# - Prefer job_workflow_ref (reliable); keep exact sub allow-list too.
#
# After apply, set GitHub Actions variables:
#   AWS_CI_ROLE_ARN = (terraform output ci_role_arn)
#   TF_STATE_BUCKET = devops-g9-tfstate-240462142849

data "aws_iam_openid_connect_provider" "github" {
  url = "https://token.actions.githubusercontent.com"
}

data "aws_iam_policy_document" "gha_trust" {
  statement {
    sid     = "GitHubOIDCByWorkflowRef"
    effect  = "Allow"
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [data.aws_iam_openid_connect_provider.github.arn]
    }

    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }

    condition {
      test     = "StringLike"
      variable = "token.actions.githubusercontent.com:job_workflow_ref"
      values = [
        "${var.github_org}/${var.github_repo}/.github/workflows/pr.yml@*",
        "${var.github_org}/${var.github_repo}/.github/workflows/release.yml@*",
        "${var.github_org}@${var.github_owner_id}/${var.github_repo}@${var.github_repo_id}/.github/workflows/pr.yml@*",
        "${var.github_org}@${var.github_owner_id}/${var.github_repo}@${var.github_repo_id}/.github/workflows/release.yml@*",
      ]
    }
  }

  statement {
    sid     = "GitHubOIDCBySub"
    effect  = "Allow"
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [data.aws_iam_openid_connect_provider.github.arn]
    }

    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:sub"
      values = [
        "repo:${var.github_org}/${var.github_repo}:pull_request",
        "repo:${var.github_org}/${var.github_repo}:ref:refs/heads/main",
        "repo:${var.github_org}/${var.github_repo}:ref:refs/heads/platform/g1-foundation",
        "repo:${var.github_org}/${var.github_repo}:environment:sandbox",
        "repo:${var.github_org}@${var.github_owner_id}/${var.github_repo}@${var.github_repo_id}:pull_request",
        "repo:${var.github_org}@${var.github_owner_id}/${var.github_repo}@${var.github_repo_id}:ref:refs/heads/main",
        "repo:${var.github_org}@${var.github_owner_id}/${var.github_repo}@${var.github_repo_id}:ref:refs/heads/platform/g1-foundation",
        "repo:${var.github_org}@${var.github_owner_id}/${var.github_repo}@${var.github_repo_id}:environment:sandbox",
      ]
    }
  }
}

resource "aws_iam_role" "ci_deploy" {
  name               = "${var.name_prefix}-ci-deploy"
  description        = "GitHub Actions OIDC role for terraform plan and ECR/ECS deploy."
  assume_role_policy = data.aws_iam_policy_document.gha_trust.json

  tags = {
    Name    = "${var.name_prefix}-ci-deploy"
    service = "platform"
  }
}

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
          "servicediscovery:*",
          "sts:GetCallerIdentity"
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
