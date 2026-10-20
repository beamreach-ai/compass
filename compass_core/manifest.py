"""The core's own manifest: what every install has and nothing can turn off.

The Queue and Settings tabs, the routers that are not a captain's (sign-in,
tokens, status, jobs, the target, settings, notifications, Slack, the Captains
page), and the setup commands."""

from __future__ import annotations

from compass_core import __version__
from compass_core.captains import AwsPermissions, CaptainManifest, Permissions, Statement, TabSpec

MANIFEST = CaptainManifest(
    name="core",
    label="Compass",
    summary="Sign-in, API tokens, the job queue, workspace settings and the Captains page.",
    tier="open",
    feature="core",
    commands="compass_core.commands",
    routers=(
        "compass_api.routes.auth:router",
        "compass_api.routes.tokens:router",
        "compass_api.routes.status:router",
        "compass_api.routes.jobs:router",
        "compass_api.routes.target:router",
        "compass_api.routes.settings:router",
        "compass_api.routes.notifications:router",
        "compass_api.routes.slack_connect:router",
        "compass_api.routes.captains:router",
        "compass_api.routes.updates:router",
    ),
    admin_write_prefixes=("/api/captains/", "/api/updates/"),
    # the block every install needs: the self-audit reads the two Compass roles and nothing else
    permissions=Permissions(
        aws=AwsPermissions(
            read=(
                Statement(
                    sid="SelfAudit",
                    actions=("iam:GetRole", "iam:GetRolePolicy", "iam:ListRolePolicies", "iam:ListAttachedRolePolicies",
                             "iam:GetPolicy", "iam:GetPolicyVersion", "iam:SimulatePrincipalPolicy"),
                    resources=("{read_role_arn}", "{act_role_arn}"),
                ),
            ),
        ),
    ),
    tabs=(
        TabSpec(key="queue", label="Queue", group="platform", feature="queue"),
        TabSpec(key="settings", label="Settings", group="platform", feature="settings"),
    ),
    requires_core=">=0.1",
    version=__version__,
)
