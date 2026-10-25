"""The patch captain's manifest (docs/specs/patching-captain.md §3.10).

The feeds need no AWS read of their own: the inventory comes from the map the mapper wrote.
The canary's action and the reads it needs join the permissions when verification lands."""

from __future__ import annotations

from compass_core import __version__
from compass_core.captains import CaptainManifest, Permissions, ServiceSpec, TabSpec

CONFIG_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "Patches",
    "type": "object",
    "additionalProperties": True,
    "properties": {
        "act_on": {
            "type": "object", "x-order": 1, "x-group": "Pull requests without a click",
            "additionalProperties": True,
            "properties": {
                "kev": {"type": "boolean", "default": True, "x-order": 1, "x-help": "A vulnerability in CISA's known-exploited catalogue."},
                "epss_min": {"type": "number", "minimum": 0, "maximum": 1, "default": 0.1, "x-order": 2, "x-help": "EPSS at or above this probability."},
                "cvss_min": {"type": "number", "minimum": 0, "maximum": 10, "default": 7.0, "x-order": 3, "x-help": "CVSS at or above this score."},
                "exposed": {"type": "boolean", "default": True, "x-order": 4, "x-help": "The workload faces the internet."},
            },
        },
        "repos": {"type": "array", "items": {"type": "string"}, "x-order": 2, "x-help": "Repositories the captain may open pull requests in; empty means every repository the mapper sees."},
        "max_open_prs": {"type": "integer", "minimum": 0, "default": 10, "x-order": 3, "x-help": "Open fix pull requests at a time; beyond it, findings wait in the inbox."},
    },
}

MANIFEST = CaptainManifest(
    name="patch",
    label="Patches",
    summary="Follows the CVE feeds, finds every affected workload and repository, opens and verifies the fix pull request.",
    tier="open",
    feature="patches",
    commands="captains.patch.commands",
    routers=("compass_api.routes.patches:router",),
    route_prefixes=("/api/patches",),
    tabs=(TabSpec(key="patches", label="Patches", group="security", feature="patches", views=("findings", "needs_you")),),
    services=(ServiceSpec(name="cve-feed", start="captains.patch.feed:run_service", interval=3600, interval_setting="cve_feed"),
              ServiceSpec(name="patch-tick", start="captains.patch.scheduler:run_service", interval=900, interval_setting="patch_tick")),
    inbox="captains.patch.inbox:items",
    config_schema=CONFIG_SCHEMA,
    permissions=Permissions(
        github=("contents:read",),
        egress=("api.osv.dev", "osv-vulnerabilities.storage.googleapis.com", "raw.githubusercontent.com", "www.cisa.gov", "api.first.org",
                "ubuntu.com", "security-tracker.debian.org", "secdb.alpinelinux.org", "explore.alas.aws.amazon.com", "packagist.org", "registry.npmjs.org"),
    ),
    depends_on=("mapper",),
    requires_core=">=0.1,<1",
    version=__version__,
)
