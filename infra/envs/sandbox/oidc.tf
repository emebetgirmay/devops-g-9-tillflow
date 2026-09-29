# GitHub OIDC - same lab constraints as devops-g10 (approved G1).
# - Cannot create the account OIDC provider.
# - Do NOT data-source the provider by URL during plan: CI role lacks
#   iam:ListOpenIDConnectProviders. Use the well-known ARN instead.
# - Trust cannot use sub "...:*"; use job_workflow_ref + exact subs.
#
# After apply, set GitHub Actions variables:
#   AWS_CI_ROLE_ARN = (terraform output ci_role_arn)
#   TF_STATE_BUCKET = devops-g9-tfstate-240462142849

locals {
  github_oidc_provider_arn = "arn:aws:iam::${local.account_id}:oidc-provider/token.actions.githubusercontent.com"
}

data "aws_iam_policy_document" "gha_trust" {
  statement {
    sid     = "GitHubOIDCByWorkflowRef"
    effect  = "Allow"
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [local.github_oidc_provider_arn]
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
      identifiers = [local.github_oidc_provider_arn]
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
          "apigateway:*",
          "logs:*",
          "iam:GetRole",
          "iam:GetRolePolicy",
          "iam:ListRolePolicies",
          "iam:ListAttachedRolePolicies",
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
        # G3 Slack path (ADR 0010): alarms -> SNS -> Lambda. Own names only.
        Sid    = "ReliabilityAlerting"
        Effect = "Allow"
        Action = ["sns:*", "lambda:*"]
        Resource = [
          "arn:aws:sns:${var.aws_region}:${local.account_id}:${var.name_prefix}-*",
          "arn:aws:lambda:${var.aws_region}:${local.account_id}:function:${var.name_prefix}-*"
        ]
      },
      {
        # Probe and Commission schedules (ADR 0010, G2 Commission).
        Sid      = "OwnSchedules"
        Effect   = "Allow"
        Action   = ["scheduler:*"]
        Resource = "arn:aws:scheduler:${var.aws_region}:${local.account_id}:schedule/*/${var.name_prefix}-*"
      },
      {
        # Slack webhook secret: CI creates and describes it, never reads or writes the value.
        Sid    = "ManageOwnSecretsMetadata"
        Effect = "Allow"
        Action = [
          "secretsmanager:CreateSecret",
          "secretsmanager:DeleteSecret",
          "secretsmanager:DescribeSecret",
          "secretsmanager:UpdateSecret",
          "secretsmanager:TagResource",
          "secretsmanager:UntagResource",
          "secretsmanager:GetResourcePolicy"
        ]
        Resource = "arn:aws:secretsmanager:${var.aws_region}:${local.account_id}:secret:${var.name_prefix}/*"
      },
      {
        # KMS key for the SNS topic. Keys cannot be named up front, so scope by the group tag.
        Sid      = "CreateKeys"
        Effect   = "Allow"
        Action   = ["kms:CreateKey", "kms:ListAliases"]
        Resource = "*"
      },
      {
        Sid      = "TagNewKeysAndFileSystems"
        Effect   = "Allow"
        Action   = ["kms:TagResource", "elasticfilesystem:CreateFileSystem", "elasticfilesystem:TagResource"]
        Resource = "*"
        Condition = {
          StringEquals = { "aws:RequestTag/group" = var.name_prefix }
        }
      },
      {
        Sid    = "ManageOwnKeys"
        Effect = "Allow"
        Action = [
          "kms:DescribeKey",
          "kms:GetKeyPolicy",
          "kms:PutKeyPolicy",
          "kms:GetKeyRotationStatus",
          "kms:EnableKeyRotation",
          "kms:ListResourceTags",
          "kms:TagResource",
          "kms:UntagResource",
          "kms:ScheduleKeyDeletion",
          "kms:CreateAlias",
          "kms:DeleteAlias"
        ]
        Resource = "arn:aws:kms:${var.aws_region}:${local.account_id}:key/*"
        Condition = {
          StringEquals = { "aws:ResourceTag/group" = var.name_prefix }
        }
      },
      {
        Sid      = "ManageOwnKeyAliases"
        Effect   = "Allow"
        Action   = ["kms:CreateAlias", "kms:DeleteAlias"]
        Resource = "arn:aws:kms:${var.aws_region}:${local.account_id}:alias/${var.name_prefix}-*"
      },
      {
        # Commission ledger on EFS (G2). Describe is account-wide; changes need the group tag.
        Sid      = "DescribeFileSystems"
        Effect   = "Allow"
        Action   = ["elasticfilesystem:Describe*", "elasticfilesystem:ListTagsForResource"]
        Resource = "*"
      },
      {
        Sid      = "ManageOwnFileSystems"
        Effect   = "Allow"
        Action   = ["elasticfilesystem:*"]
        Resource = "*"
        Condition = {
          StringEquals = { "aws:ResourceTag/group" = var.name_prefix }
        }
      },
      {
        Sid    = "PassNamespacedRoles"
        Effect = "Allow"
        Action = ["iam:PassRole"]
        Resource = [
          "arn:aws:iam::${local.account_id}:role/${var.name_prefix}-*"
        ]
      },
      {
        Sid      = "CreateServiceLinkedRoles"
        Effect   = "Allow"
        Action   = ["iam:CreateServiceLinkedRole"]
        Resource = "*"
        Condition = {
          StringEquals = {
            "iam:AWSServiceName" = [
              "ecs.amazonaws.com",
              "ecs.application-autoscaling.amazonaws.com",
              "elasticloadbalancing.amazonaws.com",
              "elasticfilesystem.amazonaws.com"
            ]
          }
        }
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
