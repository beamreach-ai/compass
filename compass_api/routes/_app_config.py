from __future__ import annotations

import os
from typing import Any, Dict, Iterable, Optional

from fastapi import HTTPException, Request

from compass_api import extensions
from compass_core import captains as _captains
from compass_core.features import (
    disabled_features_from_env,
    is_feature_enabled,
)
from compass_core.features import (
    enabled_tabs as feature_enabled_tabs,
)


def _manifest_tabs() -> tuple:
    """(key, label, group, captain, views, feature) for every tab a manifest declares, in manifest order."""
    return tuple(
        {"key": tab.key, "label": tab.label, "group": tab.group, "captain": m.name, "views": list(tab.views), "feature": tab.feature}
        for m, tab in _captains.tabs()
    )


def _all_tabs() -> tuple:
    # the product's tabs first (its default tab is among them), then the manifests'
    return tuple(dict.fromkeys(tuple(extensions.tabs()) + tuple(t["key"] for t in _manifest_tabs())))


def tab_details(enabled: Optional[Iterable[str]] = None) -> list:
    """What the sidebar renders: the manifests' tabs with their group and label, the
    product's with what register_tabs knows (key and feature)."""
    wanted = set(enabled) if enabled is not None else None
    details = list(_manifest_tabs())
    known = {t["key"] for t in details}
    for key, feature in extensions.tabs().items():
        if key not in known:
            details.append({"key": key, "label": key.replace("_", " ").title(), "group": "platform", "captain": None, "views": [], "feature": feature})
    order = {key: i for i, key in enumerate(_all_tabs())}
    details.sort(key=lambda t: order.get(t["key"], len(order)))
    return [t for t in details if wanted is None or t["key"] in wanted]


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off", ""}


def get_app_config(config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """The app's switches. With the workspace ``config``, the tabs of captains that are
    not active in that workspace are left out of ``enabled_tabs`` too."""
    app_mode = (os.getenv("APP_MODE") or "production").strip().lower()
    if app_mode not in {"production", "demo"}:
        app_mode = "production"

    read_only = _env_bool("READ_ONLY", False)
    disabled_features = disabled_features_from_env()
    all_tabs = _all_tabs()
    enabled_tabs = feature_enabled_tabs(all_tabs, disabled_features=disabled_features, tab_features=extensions.tabs())
    if config is not None:
        active = _captains.active(config, disabled_features=disabled_features)
        owner = {t["key"]: t["captain"] for t in _manifest_tabs()}
        enabled_tabs = [tab for tab in enabled_tabs if owner.get(tab) is None or owner[tab] in active]
    # the mode's default, else the production default, else the first tab the workspace has
    preferred_default_tab = extensions.default_tab(app_mode) or "maps"
    for candidate in (preferred_default_tab, extensions.default_tab("production"), enabled_tabs[0] if enabled_tabs else None):
        if candidate and candidate in enabled_tabs:
            preferred_default_tab = candidate
            break

    requested_default_tab = (os.getenv("DEFAULT_TAB") or "").strip().lower()
    default_tab = requested_default_tab or preferred_default_tab
    if default_tab not in all_tabs or default_tab not in enabled_tabs:
        default_tab = preferred_default_tab

    production_url = (os.getenv("PRODUCTION_URL") or "").strip()
    workspace_identity_mode = (os.getenv("WORKSPACE_IDENTITY_MODE") or "user").strip().lower()

    return {
        "mode": app_mode,
        "read_only": read_only,
        "settings_enabled": (not read_only) and is_feature_enabled("settings", disabled_features=disabled_features),
        "default_tab": default_tab,
        "production_url": production_url,
        "disabled_features": sorted(disabled_features),
        "enabled_tabs": enabled_tabs,
        "tabs": tab_details(enabled_tabs),
        "default_workspace": (os.getenv("DEFAULT_WORKSPACE") or "").strip(),
        "workspace_identity_mode": workspace_identity_mode if workspace_identity_mode in {"user", "shared"} else "user",
        "workspace_auto_provision": _env_bool("WORKSPACE_AUTO_PROVISION", True),
    }


def ensure_not_read_only(detail: str = "This environment is read-only.") -> None:
    if get_app_config()["read_only"]:
        raise HTTPException(status_code=403, detail=detail)


_WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def write_guard(*exempt_paths: str):
    """Router dependency: refuse writes when the environment is read-only.

    Reads pass; ``exempt_paths`` are writes the pipeline itself needs (an
    inbound alert must land even on a read-only demo stand).
    """
    exempt = frozenset(exempt_paths)

    def _guard(request: Request) -> None:
        if request.method in _WRITE_METHODS and request.url.path not in exempt:
            ensure_not_read_only()

    return _guard


def ensure_settings_enabled(detail: str = "Settings are disabled in this environment.") -> None:
    if not get_app_config()["settings_enabled"]:
        raise HTTPException(status_code=403, detail=detail)


def ensure_feature_enabled(feature: str, detail: str | None = None) -> None:
    if is_feature_enabled(feature):
        return
    message = detail or f"{feature.capitalize()} is disabled in this environment."
    raise HTTPException(status_code=403, detail=message)
