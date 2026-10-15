from __future__ import annotations

import os
from typing import Dict, Iterable, Optional, Set

_FEATURE_ALIASES = {
    "audit": "security",
    "audits": "security",
    "security": "security",
    "settings": "settings",
    "queue": "queue",
    "compliance": "compliance",
    "finops": "finops",
    "maps": "maps",
    "iac": "iac",
    "prs": "prs",
    "policies": "policies",
}

# tab → the feature flag that hides it. The manifests declare their tabs
# (compass_core.captains.tab_features); this static table keeps the legacy keys
# answering when no manifest is loaded. A deployment passes the tabs it adds as
# `tab_features` (compass_api.routes._app_config does, from compass_api.extensions).
_LEGACY_TAB_FEATURES = {
    "honeypots": "honeypots",
    "queue": "queue",
    "maps": "maps",
    "iac": "iac",
    "prs": "prs",
    "settings": "settings",
}


def _tab_features() -> Dict[str, str]:
    try:
        from compass_core import captains as _captains

        return {**_LEGACY_TAB_FEATURES, **_captains.tab_features()}
    except Exception:  # noqa: BLE001 - the flags answer even when discovery cannot run
        return dict(_LEGACY_TAB_FEATURES)

_DOMAIN_FEATURES = {
    "security": "security",
    "finops": "finops",
}


def normalize_feature_name(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    normalized = str(value).strip().lower().replace("-", "_")
    if not normalized:
        return None
    return _FEATURE_ALIASES.get(normalized, normalized)


def disabled_features_from_env(raw_value: Optional[str] = None) -> Set[str]:
    source = os.getenv("DISABLED_FEATURES", "") if raw_value is None else raw_value
    disabled: Set[str] = set()
    for token in str(source).split(","):
        feature = normalize_feature_name(token)
        if feature:
            disabled.add(feature)
    return disabled


def is_feature_enabled(feature: Optional[str], disabled_features: Optional[Set[str]] = None) -> bool:
    normalized = normalize_feature_name(feature)
    if not normalized:
        return True
    disabled = disabled_features if disabled_features is not None else disabled_features_from_env()
    return normalized not in disabled


def feature_for_tab(tab: Optional[str], tab_features: Optional[Dict[str, str]] = None) -> Optional[str]:
    if tab is None:
        return None
    return {**_tab_features(), **(tab_features or {})}.get(str(tab).strip().lower())


def is_tab_enabled(tab: Optional[str], disabled_features: Optional[Set[str]] = None, tab_features: Optional[Dict[str, str]] = None) -> bool:
    feature = feature_for_tab(tab, tab_features)
    return is_feature_enabled(feature, disabled_features=disabled_features)


def enabled_tabs(tabs: Iterable[str], disabled_features: Optional[Set[str]] = None, tab_features: Optional[Dict[str, str]] = None) -> list[str]:
    return [tab for tab in tabs if is_tab_enabled(tab, disabled_features=disabled_features, tab_features=tab_features)]


def feature_for_domain(domain: Optional[str]) -> Optional[str]:
    if domain is None:
        return None
    return _DOMAIN_FEATURES.get(str(domain).strip().lower())


def is_domain_enabled(domain: Optional[str], disabled_features: Optional[Set[str]] = None) -> bool:
    feature = feature_for_domain(domain)
    return is_feature_enabled(feature, disabled_features=disabled_features)
