"""Captains as extensions (docs/captain_extensions_spec.md §2).

A captain is the unit a user installs, turns on or off, and configures. Its
manifest says what it is made of: the module that registers its commands, the
API routers and the paths that belong to it, its dashboard tabs, the queue the
product runs it on, the background services ``compass serve`` runs for it, the
section of the workspace config it reads and the schema of that section, its
tier, and the core version it needs. Everything that used to spell a captain
out by name (the command tables, the queue map, the tab tables, the router
lists, the server's loops) reads this registry instead.

Discovery: the ``compass.captains`` entry-point group of every installed
distribution; ``COMPASS_CAPTAINS`` (comma-separated ``module:ATTR`` references)
for a checkout that is not installed; the manifests this distribution ships
(``_SHIPPED``); and ``register()`` for a host that assembles its own set before
it is a distribution of its own. A manifest whose ``requires_core`` rejects this
core is listed as incompatible and never loaded.

State: three layers resolved per workspace into one ``CaptainState``:
installed (discovered and compatible), licensed (``compass_core.licence``) and
enabled (the ``captains`` section of the workspace config, with
``DISABLED_FEATURES`` as a deploy-level veto). ``ensure_active`` is the one
check the queue, the dispatcher and the API call before work starts; reads
never go through it.
"""

from __future__ import annotations

import importlib
import logging
import os
import re
import threading
from dataclasses import dataclass, field, replace
from importlib import metadata
from typing import Any, Callable, Dict, FrozenSet, Iterable, List, Literal, Mapping, Optional, Tuple

from compass_core import __version__ as _core_version
from compass_core import permissions as _permissions
from compass_core.permissions import AwsPermissions, Permissions, Statement, permission_errors  # noqa: F401 - re-exported for manifests

logger = logging.getLogger("compass.captains")

ENTRY_POINT_GROUP = "compass.captains"
CORE = "core"
TIERS: Tuple[str, ...] = ("open", "cloud", "enterprise")
TAB_GROUPS: Tuple[str, ...] = ("cost", "security", "platform")
Tier = Literal["open", "cloud", "enterprise"]

_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")

# The manifests this distribution ships, so a checkout that is not pip-installed
# (tests, `uvicorn --reload` in compose) finds the same set the entry points name.
_SHIPPED: Tuple[str, ...] = (
    "compass_core.manifest:MANIFEST",
    "captains.mapper.manifest:MANIFEST",
    "captains.librarian.manifest:MANIFEST",
    "captains.honeypot.manifest:MANIFEST",
    "captains.iac.manifest:MANIFEST",
    "captains.prs.manifest:MANIFEST",
)


# ── the manifest ────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class TabSpec:
    key: str                      # URL and bootstrap key: "honeypots"
    label: str                    # "Decoys"
    group: str                    # sidebar group: cost | security | platform
    feature: str                  # the DISABLED_FEATURES key that hides it
    views: Tuple[str, ...] = ()   # sub-views the sidebar lists under the tab


@dataclass(frozen=True)
class ServiceSpec:
    """A background loop ``compass serve`` runs for an active captain.

    ``start`` names a callable ``(workspace_id, config, stop_event, *, interval)``
    that returns when ``stop_event`` is set. A loop ticks first and then waits
    on the event, so a server started with ``once`` (the event already set)
    gets exactly one tick. ``interval_setting`` names the serve payload key or
    environment variable (upper-cased) that gives the interval in seconds; a
    service whose interval resolves to 0 is not started."""

    name: str
    start: str
    interval: Optional[int] = None
    interval_setting: Optional[str] = None


@dataclass(frozen=True)
class CaptainManifest:
    name: str
    label: str
    summary: str
    tier: str
    feature: str
    commands: Optional[str] = None                 # dotted module whose import registers (captain, command) handlers
    routers: Tuple[str, ...] = ()                  # "package.module:router", included in this order
    router_prefixes: Mapping[str, str] = field(default_factory=dict)  # router ref -> mount prefix, when not "/"
    route_prefixes: Tuple[str, ...] = ()           # request paths that belong to this captain
    admin_write_prefixes: Tuple[str, ...] = ()     # writes under these need the admin token scope
    tabs: Tuple[TabSpec, ...] = ()
    queue: str = "captain-executions"              # the product's Celery queue; `compass serve` ignores it
    services: Tuple[ServiceSpec, ...] = ()
    config_section: Optional[str] = None           # workspace-config key; None = name
    config_schema: Optional[Mapping[str, Any]] = None
    settings_component: Optional[str] = None       # dashboard component key for custom settings UI
    depends_on: Tuple[str, ...] = ()
    enabled_by_default: bool = True
    requires_core: str = ">=0.1"
    version: str = "0.0.0"
    install_hint: Optional[str] = None             # how to install it when a licence names it and it is missing
    permissions: Optional[Permissions] = None      # what it reads in the target account and what it may propose to do (compass_core.permissions)

    @property
    def section(self) -> str:
        return self.config_section or self.name

    @property
    def is_core(self) -> bool:
        return self.name == CORE

    def errors(self) -> List[str]:
        """What is wrong with this manifest; empty when it is valid."""
        problems: List[str] = []
        if not _NAME_RE.match(self.name or ""):
            problems.append(f"name {self.name!r} is not lower_snake_case")
        if self.tier not in TIERS:
            problems.append(f"tier {self.tier!r} is not one of {TIERS}")
        if not self.label or not self.summary:
            problems.append("label and summary are required")
        if not self.feature:
            problems.append("feature is required")
        for tab in self.tabs:
            if tab.group not in TAB_GROUPS:
                problems.append(f"tab {tab.key!r} group {tab.group!r} is not one of {TAB_GROUPS}")
        for ref in self.routers:
            if ":" not in ref:
                problems.append(f"router {ref!r} is not module:attribute")
        for ref in self.router_prefixes or {}:
            if ref not in self.routers:
                problems.append(f"router_prefixes names {ref!r}, which is not in routers")
        for service in self.services:
            if ":" not in service.start:
                problems.append(f"service {service.name!r} start {service.start!r} is not module:attribute")
        for prefix in self.route_prefixes + self.admin_write_prefixes:
            if not prefix.startswith("/"):
                problems.append(f"prefix {prefix!r} does not start with /")
        if self.name in self.depends_on:
            problems.append("a captain cannot depend on itself")
        try:
            _specifier(self.requires_core)
        except ValueError as exc:
            problems.append(f"requires_core {self.requires_core!r}: {exc}")
        problems.extend(f"permissions: {p}" for p in permission_errors(self.permissions))
        return problems


# ── discovery ───────────────────────────────────────────────────────────────

_lock = threading.RLock()
_discovered: Optional[Dict[str, CaptainManifest]] = None
_registered: Dict[str, CaptainManifest] = {}
_incompatible: Dict[str, str] = {}   # name -> why it was not loaded
_broken: Dict[str, str] = {}         # reference -> import error


def core_version() -> str:
    return _core_version


def _specifier(spec: str):
    from packaging.specifiers import InvalidSpecifier, SpecifierSet

    try:
        return SpecifierSet(spec or "")
    except InvalidSpecifier as exc:
        raise ValueError(str(exc)) from exc


def incompatibility(manifest: CaptainManifest) -> Optional[str]:
    """Why this core cannot run the manifest, or None."""
    try:
        spec = _specifier(manifest.requires_core)
    except ValueError as exc:
        return f"requires_core is invalid: {exc}"
    if not spec.contains(core_version(), prereleases=True):
        return f"needs compass_core {manifest.requires_core}; this is {core_version()}"
    return None


def load_reference(ref: str) -> Any:
    """``module:attribute`` → the attribute."""
    module_name, _, attr = ref.partition(":")
    if not module_name or not attr:
        raise ValueError(f"{ref!r} is not module:attribute")
    return getattr(importlib.import_module(module_name), attr)


def _candidates() -> List[Tuple[str, Callable[[], Any]]]:
    found: List[Tuple[str, Callable[[], Any]]] = []
    try:
        for ep in metadata.entry_points(group=ENTRY_POINT_GROUP):
            found.append((f"{ep.module}:{ep.attr}" if ep.attr else ep.value, ep.load))
    except Exception as exc:  # noqa: BLE001 - a broken distribution never hides the rest
        logger.warning("captain entry points could not be read: %s", exc)
    for ref in (os.getenv("COMPASS_CAPTAINS") or "").split(","):
        ref = ref.strip()
        if ref:
            found.append((ref, lambda ref=ref: load_reference(ref)))
    for ref in _SHIPPED:
        found.append((ref, lambda ref=ref: load_reference(ref)))
    return found


def _admit(table: Dict[str, CaptainManifest], manifest: Any, *, source: str) -> None:
    if not isinstance(manifest, CaptainManifest):
        _broken[source] = f"{source} is not a CaptainManifest"
        logger.warning("%s is not a CaptainManifest; skipped", source)
        return
    problems = manifest.errors()
    if problems:
        _broken[source] = "; ".join(problems)
        logger.warning("captain manifest %s skipped: %s", source, "; ".join(problems))
        return
    if manifest.name in table:
        return  # first source wins: entry points, then COMPASS_CAPTAINS, then shipped
    reason = incompatibility(manifest)
    if reason:
        _incompatible[manifest.name] = reason
        logger.warning("captain %s not loaded: %s", manifest.name, reason)
    table[manifest.name] = manifest


def discover(*, refresh: bool = False) -> Dict[str, CaptainManifest]:
    """Every manifest on this Python path, by name, in discovery order; cached per process.

    Incompatible manifests are in the table too (so the Captains page can list
    them) but ``compatible()`` is False for them and nothing of theirs is loaded."""
    global _discovered
    with _lock:
        if _discovered is not None and not refresh:
            return dict(_discovered)
        _incompatible.clear()
        _broken.clear()
        table: Dict[str, CaptainManifest] = {}
        for source, loader in _candidates():
            try:
                _admit(table, loader(), source=source)
            except Exception as exc:  # noqa: BLE001
                _broken[source] = f"{type(exc).__name__}: {exc}"
                logger.warning("captain manifest %s failed to load: %s", source, exc)
        for manifest in _registered.values():
            _admit(table, manifest, source=f"registered:{manifest.name}")
        # the core first, then the rest in discovery order
        ordered = {name: table[name] for name in sorted(table, key=lambda n: (n != CORE, list(table).index(n)))}
        _discovered = ordered
        return dict(ordered)


def register(manifest: CaptainManifest) -> None:
    """Add a manifest without a distribution (a host that is not yet packaged; tests).

    A registered manifest replaces a discovered one of the same name."""
    problems = manifest.errors()
    if problems:
        raise ValueError(f"captain manifest {manifest.name!r}: " + "; ".join(problems))
    with _lock:
        _registered[manifest.name] = manifest
        global _discovered
        if _discovered is not None:
            _discovered = None  # re-resolve on next use


def unregister(name: str) -> None:
    with _lock:
        _registered.pop(name, None)
        global _discovered
        _discovered = None


def reset_for_tests() -> None:
    with _lock:
        _registered.clear()
        global _discovered
        _discovered = None


def manifest(name: str) -> Optional[CaptainManifest]:
    return discover().get(name)


def compatible(name: str) -> bool:
    return name in discover() and name not in _incompatible


def incompatible() -> Dict[str, str]:
    discover()
    return dict(_incompatible)


def broken() -> Dict[str, str]:
    discover()
    return dict(_broken)


def manifests(*, compatible_only: bool = True) -> List[CaptainManifest]:
    found = discover()
    return [m for name, m in found.items() if not compatible_only or name not in _incompatible]


# ── derived tables (what used to be spelled out per captain) ───────────────

_queue_overrides: Dict[str, str] = {}


def set_queue_overrides(overrides: Optional[Mapping[str, str]]) -> None:
    """The host's routing: captains that ride another's queue (the product's prs and
    finops_engine on engine-executions). Applied by ``queue_map()``."""
    _queue_overrides.clear()
    _queue_overrides.update(overrides or {})


def routers() -> List[Tuple[Any, str]]:
    """(router, mount prefix) for every router the compatible manifests declare: the
    core's first, each captain's in its manifest's order."""
    found: List[Tuple[Any, str]] = []
    for m in manifests():
        for ref in m.routers:
            found.append((load_reference(ref), str((m.router_prefixes or {}).get(ref, "") or "")))
    return found

def commands_modules() -> Dict[str, str]:
    """captain → the module whose import registers its commands."""
    return {m.name: m.commands for m in manifests() if m.commands}


def load_commands(name: str) -> None:
    """Import the captain's commands module and register its ``COMMANDS`` (idempotent;
    a second call after the dispatch table was reset registers them again)."""
    found = commands_modules().get(name)
    if found is None:
        raise LookupError(f"unknown captain {name!r}; known: {', '.join(sorted(commands_modules()))}")
    module = importlib.import_module(found)
    table = getattr(module, "COMMANDS", None)
    if table:
        from compass_core import dispatch

        dispatch.register(name, table)


def load_all_commands() -> None:
    for name in commands_modules():
        load_commands(name)


def commands_of(name: str) -> Dict[str, str]:
    """A captain's commands and the first line of each handler's docstring, after
    loading its commands module; empty for a captain with none."""
    from compass_core import dispatch

    if name not in commands_modules():
        return {}
    try:
        load_commands(name)
    except Exception as exc:  # noqa: BLE001 - a captain whose module fails to import has no commands to offer
        logger.warning("commands of %s could not be loaded: %s", name, exc)
        return {}
    out: Dict[str, str] = {}
    for captain, command in dispatch.commands():
        if captain != name:
            continue
        doc = (getattr(dispatch._registry[(captain, command)], "__doc__", None) or "").strip()
        out[command] = doc.split("\n", 1)[0].strip()
    return out


def tab_features() -> Dict[str, str]:
    """tab key → feature flag, over every compatible manifest."""
    return {tab.key: tab.feature for m in manifests() for tab in m.tabs}


def tabs() -> List[Tuple[CaptainManifest, TabSpec]]:
    return [(m, tab) for m in manifests() for tab in m.tabs]


def queue_map(overrides: Optional[Mapping[str, str]] = None) -> Dict[str, str]:
    """captain → queue name, with the host's routing applied (``set_queue_overrides``, then ``overrides``)."""
    table = {m.name: m.queue for m in manifests() if not m.is_core}
    table.update(_queue_overrides)
    table.update(overrides or {})
    return table


def captain_for_path(path: str) -> Optional[CaptainManifest]:
    """The captain a request path belongs to, by the longest declared prefix; None for core paths."""
    best: Optional[Tuple[int, CaptainManifest]] = None
    for m in manifests():
        if m.is_core:
            continue
        for prefix in m.route_prefixes:
            if (path == prefix or path.startswith(prefix.rstrip("/") + "/")) and (best is None or len(prefix) > best[0]):
                best = (len(prefix), m)
    return best[1] if best else None


# ── state ───────────────────────────────────────────────────────────────────

REASONS = ("incompatible", "unlicensed", "disabled_by_deploy", "disabled_by_admin", "dependency_off")


@dataclass(frozen=True)
class CaptainState:
    manifest: CaptainManifest
    installed: bool = True
    compatible: bool = True
    licensed: bool = True
    enabled: bool = True
    blocked_by: Tuple[str, ...] = ()
    reason: Optional[str] = None

    @property
    def name(self) -> str:
        return self.manifest.name

    @property
    def active(self) -> bool:
        return self.installed and self.compatible and self.licensed and self.enabled and not self.blocked_by

    @property
    def code(self) -> str:
        """The refusal code the API and CLI switch on, from the first reason that applies."""
        return {
            "incompatible": "captain_incompatible",
            "unlicensed": "captain_unlicensed",
            "dependency_off": "dependency_off",
        }.get(self.reason or "", "captain_disabled")

    def to_dict(self) -> Dict[str, Any]:
        m = self.manifest
        return {
            "name": m.name,
            "label": m.label,
            "summary": m.summary,
            "tier": m.tier,
            "feature": m.feature,
            "version": m.version,
            "requires_core": m.requires_core,
            "tabs": [{"key": t.key, "label": t.label, "group": t.group, "feature": t.feature, "views": list(t.views)} for t in m.tabs],
            "depends_on": list(m.depends_on),
            "has_config": bool(m.config_schema),
            "settings_component": m.settings_component,
            "core": m.is_core,
            "installed": self.installed,
            "compatible": self.compatible,
            "licensed": self.licensed,
            "enabled": self.enabled,
            "blocked_by": list(self.blocked_by),
            "active": self.active,
            "reason": self.reason,
            "install_hint": m.install_hint,
            "permissions": _permissions.describe(m.permissions),
        }


def _captains_section(config: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    section = (config or {}).get("captains") if isinstance(config, Mapping) else None
    return dict(section) if isinstance(section, Mapping) else {}


def admin_enabled(config: Optional[Mapping[str, Any]], manifest: CaptainManifest) -> bool:
    """The admin's toggle for this captain: the config's ``captains.<name>.enabled``, else the manifest default."""
    if manifest.is_core:
        return True
    entry = _captains_section(config).get(manifest.name)
    if isinstance(entry, Mapping) and isinstance(entry.get("enabled"), bool):
        return bool(entry["enabled"])
    return manifest.enabled_by_default


def states(
    config: Optional[Mapping[str, Any]],
    *,
    licence: Any = None,
    disabled_features: Optional[Iterable[str]] = None,
) -> Dict[str, CaptainState]:
    """Every discovered captain's state for this workspace (the core included, always active)."""
    from compass_core import features as _features
    from compass_core import licence as _licence

    lic = licence if licence is not None else _licence.licence_for(config)
    disabled = set(disabled_features) if disabled_features is not None else _features.disabled_features_from_env()
    disabled = {_features.normalize_feature_name(f) for f in disabled} - {None}

    out: Dict[str, CaptainState] = {}
    for m in discover().values():
        reason: Optional[str] = None
        is_compatible = m.name not in _incompatible
        is_licensed = lic.is_licensed(m)
        enabled = admin_enabled(config, m)
        vetoed = (not m.is_core) and _features.normalize_feature_name(m.feature) in disabled
        if not is_compatible:
            reason = "incompatible"
        elif not is_licensed:
            reason = "unlicensed"
        elif vetoed:
            reason = "disabled_by_deploy"
        elif not enabled:
            reason = "disabled_by_admin"
        out[m.name] = CaptainState(
            manifest=m, compatible=is_compatible, licensed=is_licensed, enabled=enabled and not vetoed, reason=reason,
        )

    # dependencies: a captain whose dependency is off is blocked; iterate to a fixpoint for chains
    changed = True
    while changed:
        changed = False
        for name, state in list(out.items()):
            blocked = tuple(dep for dep in state.manifest.depends_on if not (dep in out and out[dep].active))
            if blocked != state.blocked_by:
                out[name] = replace(state, blocked_by=blocked, reason=state.reason or ("dependency_off" if blocked else None))
                changed = True
    return out


def state_of(name: str, config: Optional[Mapping[str, Any]], **kwargs: Any) -> Optional[CaptainState]:
    return states(config, **kwargs).get(name)


def active(config: Optional[Mapping[str, Any]], **kwargs: Any) -> FrozenSet[str]:
    return frozenset(name for name, state in states(config, **kwargs).items() if state.active)


def is_active(name: str, config: Optional[Mapping[str, Any]], **kwargs: Any) -> bool:
    """False only for a known, inactive captain. Unknown names are not this registry's to refuse."""
    state = state_of(name, config, **kwargs)
    return True if state is None else state.active


def required_by(name: str, config: Optional[Mapping[str, Any]], **kwargs: Any) -> Tuple[str, ...]:
    """Active captains that depend on ``name`` (what disabling it would block)."""
    return tuple(s.name for s in states(config, **kwargs).values() if name in s.manifest.depends_on and s.active)


# ── the check ───────────────────────────────────────────────────────────────

class CaptainUnavailable(RuntimeError):
    """Work was asked of a captain this workspace cannot run right now."""

    def __init__(self, state: CaptainState):
        self.state = state
        self.captain = state.name
        self.code = state.code
        self.reason = state.reason
        self.blocked_by = state.blocked_by
        super().__init__(describe(state))

    def to_dict(self) -> Dict[str, Any]:
        return {"code": self.code, "captain": self.captain, "reason": self.reason, "blocked_by": list(self.blocked_by), "detail": str(self)}


def describe(state: CaptainState) -> str:
    label = state.manifest.label
    if not state.compatible:
        return f"{label} is not compatible with this Compass ({_incompatible.get(state.name) or 'version mismatch'})."
    if not state.licensed:
        return f"{label} is not included in this workspace's licence."
    if state.blocked_by:
        return f"{label} needs {', '.join(state.blocked_by)} turned on."
    if state.reason == "disabled_by_deploy":
        return f"{label} is disabled in this environment."
    return f"{label} is turned off for this workspace."


def ensure_active(name: str, config: Optional[Mapping[str, Any]], **kwargs: Any) -> None:
    state = state_of(name, config, **kwargs)
    if state is not None and not state.active:
        raise CaptainUnavailable(state)


def ensure_active_for_payload(name: str, payload: Optional[Mapping[str, Any]]) -> None:
    """The check at enqueue and dispatch: the workspace comes from the payload.

    A captain this registry does not know passes; so does a payload whose
    workspace has no config (the handler will say so in its own words)."""
    if name not in discover():
        return
    from compass_core.workspace import resolve

    try:
        workspace = resolve(dict(payload or {}))
    except LookupError as exc:
        logger.debug("captain check skipped for %s: %s", name, exc)
        return
    ensure_active(name, workspace.config)
