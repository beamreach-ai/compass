"""Version identifiers the CLI and MCP server use to check compatibility."""

from __future__ import annotations

import os

# Bump when the HTTP API changes in a way the CLI must know about.
API_VERSION = "2026.10.1"


def compass_version() -> str:
    """Image/build version of this Compass install (set by the stack from the image tag)."""
    return (os.getenv("COMPASS_VERSION") or os.getenv("IMAGE_TAG") or "dev").strip() or "dev"
