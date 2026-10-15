"""The core's own manifest: what every install has and nothing can turn off.

The Queue and Settings tabs, the routers that are not a captain's (sign-in,
tokens, status, jobs, the target, settings, notifications, Slack, the Captains
page), and the setup commands."""

from __future__ import annotations

from compass_core import __version__
from compass_core.captains import CaptainManifest, TabSpec

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
    ),
    admin_write_prefixes=("/api/captains/",),
    tabs=(
        TabSpec(key="queue", label="Queue", group="platform", feature="queue"),
        TabSpec(key="settings", label="Settings", group="platform", feature="settings"),
    ),
    requires_core=">=0.1",
    version=__version__,
)
