"""What the inventory reads: the mapper's declared permissions, shared with the IaC captain,
whose ``terraform plan`` refreshes the same resources (docs/specs/access-model.md §5)."""

from __future__ import annotations

from compass_core.permissions import AwsPermissions, Permissions, Statement

INVENTORY_READS = (
    Statement(
        sid="Inventory",
        actions=(
            "ec2:Describe*",
            "ecs:Describe*", "ecs:List*",
            "lambda:List*",
            "iam:Get*", "iam:List*",
            "s3:ListAllMyBuckets", "s3:GetBucket*",
            "rds:Describe*",
            "dynamodb:List*", "dynamodb:Describe*",
            "sqs:List*", "sqs:GetQueueAttributes",
            "sns:List*",
            "events:List*", "events:Describe*",
            "states:List*",
            "logs:Describe*",
            "cloudwatch:Describe*",
            "ecr:Describe*",
            "elasticloadbalancing:Describe*",
            "secretsmanager:List*",
        ),
    ),
    # the Terraform state: the one read that must stay scoped, state files hold secrets in plain text
    Statement(sid="TerraformState", actions=("s3:GetObject", "s3:ListBucket"),
              resources=("arn:aws:s3:::{state_bucket}", "arn:aws:s3:::{state_bucket}/*")),
)

PERMISSIONS = Permissions(
    aws=AwsPermissions(read=INVENTORY_READS),
    github=("contents:read",),
    egress=("api.github.com",),
)
