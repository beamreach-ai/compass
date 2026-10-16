"""Version identifiers the CLI and MCP server use to check compatibility."""

from __future__ import annotations

from compass_core import updates

# Bump when the HTTP API changes in a way the CLI must know about.
API_VERSION = "2026.10.2"


def compass_version() -> str:
    """The version that runs: the distribution's, not the environment's. ``COMPASS_VERSION``
    is what compose was asked to pull (``compass_core.updates.pinned_version``); the two can disagree."""
    return updates.running_version()
