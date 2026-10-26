"""The PR watcher's manifest (docs/captain_extensions_spec.md §2.1)."""

from __future__ import annotations

from compass_core import __version__
from compass_core.captains import CaptainManifest, Permissions, ServiceSpec, TabSpec
from compass_core.policy import MODES

CONFIG_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "Pull requests",
    "type": "object",
    "additionalProperties": True,
    "properties": {
        "watcher": {
            "type": "object", "x-order": 1, "x-group": "Watcher",
            "additionalProperties": True,
            "properties": {
                "mode": {"type": "string", "enum": list(MODES), "default": "autopilot", "x-order": 1,
                         "x-help": "off: watch only; approve: revisions wait for a human; autopilot: revise within the risk limit."},
                "risk_max": {"type": "integer", "minimum": 0, "maximum": 5, "default": 2, "x-order": 2,
                             "x-help": "Highest risk score the watcher revises on its own."},
                "max_revisions": {"type": "integer", "minimum": 0, "x-order": 3},
                "reply": {"type": "boolean", "x-order": 4, "x-help": "Reply to reviewer comments."},
                "nudge_after_days": {"type": "integer", "minimum": 0, "x-order": 5},
            },
        },
        "auto_merge": {
            "type": "object", "x-order": 2, "x-group": "Auto-merge",
            "additionalProperties": True,
            "properties": {
                "enabled": {"type": "boolean", "x-order": 1},
                "allow_types": {"type": "array", "items": {"type": "string"}, "x-order": 2},
                "repos": {"type": "array", "items": {"type": "string"}, "x-order": 3},
                "risk_max": {"type": "integer", "minimum": 0, "maximum": 5, "x-order": 4},
                "fix_rounds": {"type": "integer", "minimum": 0, "x-order": 5},
                "merge_method": {"type": "string", "enum": ["merge", "squash", "rebase"], "x-order": 6},
                "reviewer": {"type": "string", "x-order": 7},
            },
        },
    },
}

MANIFEST = CaptainManifest(
    name="prs",
    label="Pull requests",
    summary="Watches the pull requests Compass opened: syncs reviews, revises, nudges and merges within policy.",
    tier="open",
    feature="prs",
    commands="captains.prs.commands",
    routers=("compass_api.routes.prs:router",),
    route_prefixes=("/api/prs",),
    admin_write_prefixes=("/api/prs/watcher",),
    tabs=(TabSpec(key="prs", label="Pull requests", group="platform", feature="prs", views=("open", "needs_you", "history")),),
    queue="prs-executions",
    services=(ServiceSpec(name="prs-tick", start="captains.prs.scheduler:run_service", interval=300, interval_setting="prs_tick"),),
    config_schema=CONFIG_SCHEMA,
    settings_component="prs",
    permissions=Permissions(github=("contents:write", "pull_requests:write", "issues:write"), egress=("api.github.com",)),
    inbox="captains.prs.inbox:items",
    requires_core=">=0.1,<1",
    version=__version__,
)
