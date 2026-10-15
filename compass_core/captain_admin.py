"""Turning a captain on or off and editing its configuration (docs/captain_extensions_spec.md §2.3, §2.4).

What the API's ``/api/captains`` routes and the ``compass enable | disable |
configure`` commands share: the edits to the workspace config, the dependency
rules, the schema validation and the masking of secrets. Nothing here saves;
the caller does, through whatever saver its edition installed."""

from __future__ import annotations

import copy
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Tuple

from compass_core import captains as _captains
from compass_core.captains import CaptainManifest, CaptainState

MASK_PREFIX = "…"


class CaptainAdminError(ValueError):
    def __init__(self, code: str, detail: str, *, status: int = 409, names: Tuple[str, ...] = ()):
        self.code = code
        self.status = status
        self.names = names
        super().__init__(detail)

    def to_dict(self) -> Dict[str, Any]:
        return {"code": self.code, "detail": str(self), "captains": list(self.names)}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _manifest(name: str) -> CaptainManifest:
    found = _captains.manifest(name)
    if found is None:
        raise CaptainAdminError("captain_unknown", f"no captain named {name!r} is installed", status=404)
    return found


# ── on / off ────────────────────────────────────────────────────────────────

def set_enabled(
    config: Dict[str, Any],
    name: str,
    enabled: bool,
    *,
    with_dependencies: bool = False,
    cascade: bool = False,
    actor: Optional[str] = None,
) -> List[str]:
    """Edit ``config`` in place; returns the captains whose toggle changed.

    Enabling a captain whose dependency is off needs ``with_dependencies`` (the
    dependency is enabled too); disabling one that active captains depend on
    needs ``cascade`` (they are disabled too). The core cannot be disabled."""
    manifest = _manifest(name)
    if manifest.is_core:
        raise CaptainAdminError("captain_core", "the core cannot be turned off", status=400)
    states = _captains.states(config)
    state = states[name]
    changed: List[str] = []

    if enabled:
        if not state.compatible:
            raise CaptainAdminError("captain_incompatible", _captains.describe(state), status=409, names=(name,))
        if not state.licensed:
            raise CaptainAdminError("captain_unlicensed", _captains.describe(state), status=402, names=(name,))
        missing = tuple(dep for dep in manifest.depends_on if dep in states and not states[dep].active)
        if missing and not with_dependencies:
            raise CaptainAdminError("dependency_off", f"{manifest.label} needs {', '.join(missing)} turned on", names=missing)
        for dep in missing:
            changed += set_enabled(config, dep, True, with_dependencies=True, actor=actor)
    else:
        dependants = _captains.required_by(name, config)
        if dependants and not cascade:
            raise CaptainAdminError("required_by", f"{', '.join(dependants)} depend on {manifest.label}", names=dependants)
        for dependant in dependants:
            changed += set_enabled(config, dependant, False, cascade=True, actor=actor)

    section = config.setdefault("captains", {})
    if not isinstance(section, dict):
        section = {}
        config["captains"] = section
    entry = section.get(name)
    if not isinstance(entry, dict):
        entry = {}
    if entry.get("enabled") is not enabled or "enabled" not in entry:
        entry["enabled"] = enabled
        entry["enabled_at" if enabled else "disabled_at"] = _now()
        if actor:
            entry["enabled_by" if enabled else "disabled_by"] = actor
        changed.append(name)
    section[name] = entry
    return changed


# ── configuration ───────────────────────────────────────────────────────────

def _secret_paths(schema: Mapping[str, Any], prefix: Tuple[str, ...] = ()) -> List[Tuple[str, ...]]:
    found: List[Tuple[str, ...]] = []
    for key, sub in (schema.get("properties") or {}).items():
        if not isinstance(sub, Mapping):
            continue
        path = prefix + (key,)
        if sub.get("x-secret"):
            found.append(path)
        if sub.get("type") == "object":
            found += _secret_paths(sub, path)
    return found


def _get(d: Any, path: Tuple[str, ...]) -> Any:
    for key in path:
        if not isinstance(d, Mapping) or key not in d:
            return None
        d = d[key]
    return d


def _set(d: Dict[str, Any], path: Tuple[str, ...], value: Any) -> None:
    for key in path[:-1]:
        nxt = d.get(key)
        if not isinstance(nxt, dict):
            nxt = {}
            d[key] = nxt
        d = nxt
    d[path[-1]] = value


def mask(value: Any) -> str:
    text = str(value)
    return MASK_PREFIX + (text[-4:] if len(text) > 4 else "")


def masked_section(config: Mapping[str, Any], manifest: CaptainManifest) -> Dict[str, Any]:
    """The captain's section with every ``x-secret`` value replaced by its mask."""
    section = copy.deepcopy(config.get(manifest.section)) if isinstance(config.get(manifest.section), Mapping) else {}
    for path in _secret_paths(manifest.config_schema or {}):
        current = _get(section, path)
        if current not in (None, ""):
            _set(section, path, mask(current))
    return section


def validate_section(manifest: CaptainManifest, section: Mapping[str, Any]) -> None:
    schema = manifest.config_schema
    if not schema:
        return
    import jsonschema

    validator = jsonschema.Draft202012Validator(dict(schema))
    errors = sorted(validator.iter_errors(dict(section)), key=lambda e: list(e.path))
    if errors:
        first = errors[0]
        where = ".".join(str(p) for p in first.path) or "(root)"
        raise CaptainAdminError("invalid_config", f"{where}: {first.message}", status=422)


def apply_section_update(
    config: Dict[str, Any],
    manifest: CaptainManifest,
    incoming: Mapping[str, Any],
    *,
    replace: bool = False,
) -> Dict[str, Any]:
    """Merge ``incoming`` into the captain's section (or replace it), keeping a secret
    whose incoming value is its own mask, validate, and write it into ``config``.
    Returns the new section, unmasked."""
    current = config.get(manifest.section) if isinstance(config.get(manifest.section), Mapping) else {}
    new_section: Dict[str, Any] = copy.deepcopy(dict(incoming)) if replace else _deep_merge(copy.deepcopy(dict(current)), incoming)
    for path in _secret_paths(manifest.config_schema or {}):
        value = _get(new_section, path)
        if isinstance(value, str) and value.startswith(MASK_PREFIX):
            previous = _get(current, path)
            if previous is not None:
                _set(new_section, path, previous)
    validate_section(manifest, new_section)
    config[manifest.section] = new_section
    return new_section


def _deep_merge(base: Dict[str, Any], incoming: Mapping[str, Any]) -> Dict[str, Any]:
    for key, value in incoming.items():
        if isinstance(value, Mapping) and isinstance(base.get(key), dict):
            _deep_merge(base[key], value)
        elif value is None:
            base.pop(key, None)
        else:
            base[key] = copy.deepcopy(value) if isinstance(value, (dict, list)) else value
    return base


def describe_config(config: Mapping[str, Any], manifest: CaptainManifest) -> Dict[str, Any]:
    return {
        "captain": manifest.name,
        "section": manifest.section,
        "config": masked_section(config, manifest),
        "schema": dict(manifest.config_schema) if manifest.config_schema else None,
        "settings_component": manifest.settings_component,
        "secrets_masked": True,
    }


def summary(config: Mapping[str, Any], **kwargs: Any) -> List[Dict[str, Any]]:
    """What ``GET /api/captains`` and ``compass captains`` return."""
    return [state.to_dict() for state in _captains.states(config, **kwargs).values()]


def state_dict(config: Mapping[str, Any], name: str) -> Dict[str, Any]:
    state: Optional[CaptainState] = _captains.state_of(name, config)
    if state is None:
        raise CaptainAdminError("captain_unknown", f"no captain named {name!r} is installed", status=404)
    return state.to_dict()
