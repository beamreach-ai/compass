"""Where the product adds to the open API without the open API importing it.

The open routers answer for the open features. A few of their responses have
a part only the product can fill — usage and billing in the bootstrap and the
status page, findings counts, the audit suite queued when AWS access is saved,
licence checks before a fix job is restarted. Each is a registry here; the
product registers into it when its API module loads
at import time. With nothing registered, the
open API answers without those parts.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("compass.api.extensions")

# name -> fn(config, request) -> section, shown in /api/settings/bootstrap
BootstrapSection = Callable[[Dict[str, Any], Any], Any]
# fn(config, ctx, request) -> None, run first on every bootstrap (licence checks and the like)
BootstrapHook = Callable[[Dict[str, Any], Any, Any], None]
# name -> fn(request, config, workspace_id) -> section, shown in /api/status
StatusSection = Callable[[Any, Dict[str, Any], Optional[str]], Any]
# fn(request, config) -> dict of jobs, run after AWS settings are saved
AwsSavedHook = Callable[[Any, Dict[str, Any]], Dict[str, Any]]
# fn(request, job) -> None, may raise HTTPException to refuse a job restart
JobRestartGuard = Callable[[Any, Dict[str, Any]], None]

_bootstrap_sections: Dict[str, BootstrapSection] = {}
_bootstrap_hooks: List[BootstrapHook] = []
_status_sections: Dict[str, StatusSection] = {}
_aws_saved_hooks: List[AwsSavedHook] = []
_job_restart_guards: List[JobRestartGuard] = []
# Paths and prefixes the product adds to the auth gates (compass_api.middleware.simple_auth,
# compass_api.auth.scopes), request headers it reads the user identity from, and the tabs it
# adds to the dashboard (compass_api.routes._app_config).
_public_paths: List[str] = []
_gated_prefixes: List[str] = []
_admin_write_prefixes: List[str] = []
_admin_read_prefixes: List[str] = []
_identity_headers: List[str] = []
_tabs: Dict[str, str] = {}  # tab -> feature flag
_default_tabs: Dict[str, str] = {}  # app mode -> tab


def register_bootstrap_section(name: str, fn: BootstrapSection) -> None:
    _bootstrap_sections[name] = fn


def register_bootstrap_hook(fn: BootstrapHook) -> None:
    if fn not in _bootstrap_hooks:
        _bootstrap_hooks.append(fn)


def register_status_section(name: str, fn: StatusSection) -> None:
    _status_sections[name] = fn


def register_aws_saved_hook(fn: AwsSavedHook) -> None:
    if fn not in _aws_saved_hooks:
        _aws_saved_hooks.append(fn)


def register_job_restart_guard(fn: JobRestartGuard) -> None:
    if fn not in _job_restart_guards:
        _job_restart_guards.append(fn)


def register_public_paths(*paths: str) -> None:
    """Paths that need no session or token (webhooks, health of a product service)."""
    _public_paths.extend(path for path in paths if path not in _public_paths)


def register_gated_prefixes(*prefixes: str) -> None:
    """Path prefixes the session gate protects besides /api/."""
    _gated_prefixes.extend(prefix for prefix in prefixes if prefix not in _gated_prefixes)


def register_admin_write_prefixes(*prefixes: str) -> None:
    _admin_write_prefixes.extend(prefix for prefix in prefixes if prefix not in _admin_write_prefixes)


def register_admin_read_prefixes(*prefixes: str) -> None:
    _admin_read_prefixes.extend(prefix for prefix in prefixes if prefix not in _admin_read_prefixes)


def register_identity_headers(*names: str) -> None:
    """Request headers that carry the signed-in user's id (an ALB's OIDC headers, say)."""
    _identity_headers.extend(name for name in names if name not in _identity_headers)


def register_tabs(tabs: Dict[str, str], *, default: Optional[str] = None, demo_default: Optional[str] = None) -> None:
    """Dashboard tabs the product adds, each with the feature flag that can hide it."""
    _tabs.update(tabs)
    if default:
        _default_tabs["production"] = default
    if demo_default:
        _default_tabs["demo"] = demo_default


def public_paths() -> frozenset:
    return frozenset(_public_paths)


def gated_prefixes() -> tuple:
    return tuple(_gated_prefixes)


def admin_write_prefixes() -> tuple:
    return tuple(_admin_write_prefixes)


def admin_read_prefixes() -> tuple:
    return tuple(_admin_read_prefixes)


def identity_headers() -> tuple:
    return tuple(_identity_headers)


def tabs() -> Dict[str, str]:
    return dict(_tabs)


def default_tab(app_mode: str) -> Optional[str]:
    return _default_tabs.get(app_mode)


def bootstrap_sections() -> Dict[str, BootstrapSection]:
    return dict(_bootstrap_sections)


def run_bootstrap_hooks(config: Dict[str, Any], ctx: Any, request: Any) -> None:
    for hook in list(_bootstrap_hooks):
        try:
            hook(config, ctx, request)
        except Exception as exc:  # noqa: BLE001 - a hook never breaks the bootstrap
            logger.debug("bootstrap hook %s failed: %s", getattr(hook, "__name__", hook), exc)


def status_sections() -> Dict[str, StatusSection]:
    return dict(_status_sections)


def run_aws_saved_hooks(request: Any, config: Dict[str, Any]) -> Dict[str, Any]:
    jobs: Dict[str, Any] = {}
    for hook in list(_aws_saved_hooks):
        jobs.update(hook(request, config) or {})
    return jobs


def run_job_restart_guards(request: Any, job: Dict[str, Any]) -> None:
    for guard in list(_job_restart_guards):
        guard(request, job)


def reset_for_tests() -> None:
    _bootstrap_sections.clear()
    _bootstrap_hooks.clear()
    _status_sections.clear()
    _aws_saved_hooks.clear()
    _job_restart_guards.clear()
    for registry in (_public_paths, _gated_prefixes, _admin_write_prefixes, _admin_read_prefixes, _identity_headers):
        registry.clear()
    _tabs.clear()
    _default_tabs.clear()
