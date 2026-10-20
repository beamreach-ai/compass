"""Which scope an API-token call needs for a given method and path.

Cookie and ALB (Cognito) sessions are not scoped; this policy only applies to
``Authorization: Bearer cmp_...`` calls. Keep it centralized so a new route is
covered by the defaults (GET = read, writes = operate) and only the sensitive
exceptions are listed here.
"""

from __future__ import annotations

from typing import Optional

from compass_api import extensions
from compass_core import captains as _captains

_PUBLIC_PATHS = frozenset({
    "/health",
    "/readyz",
    "/livez",
    "/api/auth/login",
    "/api/auth/logout",
    "/api/auth/me",
})

# Writes under these prefixes change how Compass itself is configured.
_ADMIN_WRITE_PREFIXES = (
    "/api/auth/tokens",
    "/api/settings/aws",
    "/api/integrations/",
    "/api/notifications/ses-config",
    "/api/notifications/routing",
    "/api/honeypots/repo-default",
    "/api/honeypots/settings",
    "/api/honeypots/actions",
    "/api/prs/watcher",
    "/api/access",
)

# Reads that reveal credentials or configuration, or list other tokens.
_ADMIN_READ_PREFIXES = (
    "/api/auth/tokens",
)
_OPERATE_READ_SUFFIXES = (
    "/material",   # honeypot lure material
    "/lure",
)

_READ_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def is_public_path(path: str) -> bool:
    return path in _PUBLIC_PATHS or path in extensions.public_paths()


def required_scope(method: str, path: str) -> Optional[str]:
    """Return "read", "operate" or "admin"; None when the path needs no token scope."""
    if is_public_path(path):
        return None
    method = (method or "GET").upper()
    if method in _READ_METHODS:
        if path.startswith(_ADMIN_READ_PREFIXES + extensions.admin_read_prefixes()):
            return "admin"
        if path.endswith(_OPERATE_READ_SUFFIXES):
            return "operate"
        return "read"
    if path.startswith(_ADMIN_WRITE_PREFIXES + extensions.admin_write_prefixes() + _manifest_admin_prefixes()):
        return "admin"
    return "operate"


def _manifest_admin_prefixes() -> tuple:
    return tuple(prefix for m in _captains.manifests() for prefix in m.admin_write_prefixes)
