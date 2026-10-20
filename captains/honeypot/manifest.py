"""The decoys' manifest (docs/captain_extensions_spec.md §2.1)."""

from __future__ import annotations

from compass_core import __version__
from compass_core.captains import AwsPermissions, CaptainManifest, Permissions, ServiceSpec, Statement, TabSpec

CONFIG_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "Decoys",
    "type": "object",
    "additionalProperties": True,
    "properties": {
        "autopilot": {
            "type": "boolean", "default": False, "x-order": 1,
            "x-help": "Plant every decoy the infra map suggests, with default settings.",
        },
        "terraform_root": {
            "type": "string", "x-order": 2, "x-group": "Repository",
            "x-help": "Directory in the decoy repository where decoys are written (for example envs/prod).",
        },
        "alert_queue_url": {
            "type": "string", "x-order": 3, "x-group": "Alerts",
            "x-help": "SQS queue the decoy listeners deliver alerts to (`compass connect` creates it creates one).",
        },
        "alert_queue_urls": {
            "type": "array", "items": {"type": "string"}, "x-order": 4, "x-group": "Alerts",
            "x-help": "More alert queues, one per region the decoys live in.",
        },
        "alert_queue_arn": {"type": "string", "x-order": 5, "x-group": "Alerts"},
        "listener_image": {
            "type": "string", "x-order": 6, "x-group": "Listener",
            "x-help": "Container image of the decoy listener planted next to a service.",
        },
        "network": {
            "type": "object", "x-order": 7, "x-group": "Listener",
            "x-help": "Where a db_credential decoy's listener runs.",
            "properties": {
                "vpc_id": {"type": "string", "x-help": "The VPC (vpc-…)."},
                "subnet_ids": {"type": "array", "items": {"type": "string"}, "x-help": "Private subnets for the listener task."},
            },
        },
    },
}

# What arm, the investigation and the correlation read; the responses are actions (the whitelist)
PERMISSIONS = Permissions(
    aws=AwsPermissions(
        read=(
            Statement(
                sid="DecoyReads",
                actions=(
                    "iam:GetUser", "iam:ListAccessKeys", "iam:ListUserTags",
                    "iam:GetPolicy", "iam:GetPolicyVersion", "iam:GetRolePolicy",
                    "iam:ListRolePolicies", "iam:ListAttachedRolePolicies",
                    "events:DescribeRule", "events:ListTargetsByRule",
                    "cloudtrail:DescribeTrails", "cloudtrail:GetTrailStatus", "cloudtrail:LookupEvents",
                    "tag:GetResources",
                    "ecs:Describe*", "ecs:List*",
                    "ec2:DescribeNetworkInterfaces",
                    "s3:ListAllMyBuckets",
                    "secretsmanager:ListSecrets",
                    "rds:DescribeDBInstances",
                    "dynamodb:ListTables",
                ),
            ),
            # /api/honeypots/actions/check asks IAM what the responder role can do
            Statement(sid="ResponderCheck", actions=("iam:SimulatePrincipalPolicy",), resources=("{act_role_arn}",)),
        ),
        actions=("iam_deactivate_key", "ecs_restart_task"),   # what it may propose; each needs a grant a person accepts and merges
    ),
    github=("contents:write", "pull_requests:write"),
    egress=("api.github.com",),
)

MANIFEST = CaptainManifest(
    name="honeypot",
    label="Decoys",
    summary="Plant decoy credentials and resources; alert, investigate and respond when one is touched.",
    tier="open",
    feature="honeypots",
    commands="captains.honeypot.commands",
    routers=(
        # fixed /api/honeypots/<segment> routers before honeypots.router: its legacy
        # /api/honeypots/{id} catch-all would otherwise swallow them
        "compass_api.routes.honeypot_incidents:router",
        "compass_api.routes.honeypot_actions:router",
        "compass_api.routes.honeypots:router",
    ),
    route_prefixes=("/api/honeypots",),
    admin_write_prefixes=("/api/honeypots/repo-default", "/api/honeypots/settings", "/api/honeypots/actions"),
    tabs=(TabSpec(key="honeypots", label="Decoys", group="security", feature="honeypots", views=("overview", "honeytokens", "history", "settings")),),
    queue="honeypot-executions",
    services=(ServiceSpec(name="alert-consumer", start="captains.honeypot.alert_consumer:run_service"),),
    config_schema=CONFIG_SCHEMA,
    depends_on=("mapper",),
    permissions=PERMISSIONS,
    requires_core=">=0.1,<1",
    version=__version__,
)
