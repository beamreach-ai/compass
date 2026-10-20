"""``compass validate path=<project>``: the checks CI runs on a captain, on an author's machine
(docs/captain_development.md; docs/captain_extensions_spec.md §10, M5).

Given a project directory (the one with ``pyproject.toml``), it reads the
``compass.captains`` entry points, puts the project on ``sys.path`` the way an
install would, loads each manifest and checks what tests/test_captain_manifests.py
checks for the shipped captains: the manifest's own rules, compatibility with
this core, the commands module registering the captain, routers and services
resolving, every route under a declared prefix, a valid config schema, no clash
with an installed captain, the packaging matching the manifest, and the
permissions block: present, rendering to a session policy, and covering every
AWS call the code makes (compass_core.permissions).
"""

from __future__ import annotations

import importlib
import sys
import tomllib
from pathlib import Path
from typing import Any, Dict, List


def _check(results: List[Dict[str, Any]], name: str, ok: bool, detail: str = "") -> bool:
    results.append({"check": name, "ok": bool(ok), "detail": detail})
    return bool(ok)


def validate(project: str) -> Dict[str, Any]:
    from compass_core import captains, dispatch

    root = Path(project).expanduser().resolve()
    results: List[Dict[str, Any]] = []
    pyproject = root / "pyproject.toml"
    if not _check(results, "pyproject.toml present", pyproject.is_file(), str(pyproject)):
        return {"project": str(root), "ok": False, "checks": results}
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    project_meta = data.get("project") or {}
    points = ((project_meta.get("entry-points") or {}).get("compass.captains")) or {}
    if not _check(results, "declares compass.captains entry points", bool(points), ", ".join(points) or "none under [project.entry-points.\"compass.captains\"]"):
        return {"project": str(root), "ok": False, "checks": results}
    _check(results, "captains is a namespace package (no captains/__init__.py)", not (root / "captains" / "__init__.py").exists(),
           "remove captains/__init__.py: it would hide Compass's own captains" if (root / "captains" / "__init__.py").exists() else "")

    shipped = dict(captains.discover())
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    importlib.invalidate_caches()
    found = []
    for key, ref in points.items():
        # load the project's own modules, not copies an earlier import left in this process
        package = ".".join(ref.split(":")[0].split(".")[:2])
        for loaded in [m for m in sys.modules if m == package or m.startswith(package + ".")]:
            del sys.modules[loaded]
        try:
            manifest = captains.load_reference(ref)
        except Exception as exc:  # noqa: BLE001
            _check(results, f"{key}: manifest loads", False, f"{ref}: {type(exc).__name__}: {exc}")
            continue
        if not _check(results, f"{key}: manifest loads", isinstance(manifest, captains.CaptainManifest), ref):
            continue
        found.append(manifest)
        m = manifest
        _check(results, f"{m.name}: entry point name matches the manifest", key == m.name, f"entry point {key!r}, manifest {m.name!r}")
        problems = m.errors()
        _check(results, f"{m.name}: manifest rules", not problems, "; ".join(problems))
        why = captains.incompatibility(m)
        _check(results, f"{m.name}: compatible with compass_core {captains.core_version()}", why is None, why or m.requires_core)
        clash = shipped.get(m.name)
        _check(results, f"{m.name}: name is not taken by an installed captain", clash is None or clash.commands == m.commands,
               f"{m.name!r} is already {clash.label}" if clash and clash.commands != m.commands else "")
        taken = {t.key for other in shipped.values() if other.name != m.name for t in other.tabs}
        clashing = [t.key for t in m.tabs if t.key in taken]
        _check(results, f"{m.name}: tab keys are free", not clashing, ", ".join(clashing))
        version = str(project_meta.get("version") or "")
        _check(results, f"{m.name}: pyproject version matches the manifest", not version or version == m.version, f"pyproject {version}, manifest {m.version}")
        if m.commands:
            try:
                module = importlib.import_module(m.commands)
                table = getattr(module, "COMMANDS", None)
                if table:
                    dispatch.register(m.name, table)
                registered = sorted(c for cap, c in dispatch.commands() if cap == m.name)
                _check(results, f"{m.name}: commands module registers commands", bool(registered), ", ".join(registered) or f"{m.commands} registers nothing for {m.name!r}")
            except Exception as exc:  # noqa: BLE001
                _check(results, f"{m.name}: commands module imports", False, f"{m.commands}: {type(exc).__name__}: {exc}")
        for ref in m.routers:
            try:
                router = captains.load_reference(ref)
            except Exception as exc:  # noqa: BLE001
                _check(results, f"{m.name}: router {ref} loads", False, f"{type(exc).__name__}: {exc}")
                continue
            mount = (m.router_prefixes or {}).get(ref, "")
            uncovered = []
            for route in getattr(router, "routes", []):
                path = mount + getattr(route, "path", "")
                if not any(path == p or path.startswith(p.rstrip("/") + "/") for p in m.route_prefixes):
                    uncovered.append(path)
            _check(results, f"{m.name}: router {ref} routes are under its route_prefixes", not uncovered,
                   "add a route_prefix for: " + ", ".join(uncovered) if uncovered else "")
        for spec in m.services:
            try:
                ok = callable(captains.load_reference(spec.start))
                _check(results, f"{m.name}: service {spec.name} resolves", ok, spec.start)
            except Exception as exc:  # noqa: BLE001
                _check(results, f"{m.name}: service {spec.name} resolves", False, f"{spec.start}: {type(exc).__name__}: {exc}")
        _check(results, f"{m.name}: declares permissions", m.permissions is not None,
               "" if m.permissions is not None else "add permissions=Permissions(...) to the manifest: what it reads in AWS and what it may propose (docs/writing_a_captain.md)")
        if m.permissions is not None:
            from compass_core import permissions as _permissions

            try:
                policy = _permissions.session_policy(m.permissions, {"aws": {"account_id": "123456789012", "state_buckets": ["example"]}})
                _check(results, f"{m.name}: permissions render to a session policy", policy is not None,
                       "" if policy is not None else "the read block is over 2,048 characters inline; it will need a managed policy")
            except Exception as exc:  # noqa: BLE001
                _check(results, f"{m.name}: permissions render to a session policy", False, f"{type(exc).__name__}: {exc}")
            declared_actions = list(m.permissions.aws.actions) if m.permissions.aws else []
            if declared_actions:
                from compass_core import actions as _actions

                unregistered = [a for a in declared_actions if _actions.captain_of(a) != m.name]
                _check(results, f"{m.name}: declared action types are registered", not unregistered,
                       "register them with compass_core.actions.register in the commands module: " + ", ".join(unregistered) if unregistered else ", ".join(declared_actions))
            package_dir = root / "captains" / m.name
            if package_dir.is_dir():
                missing = _permissions.uncovered(m.permissions, _permissions.aws_calls(package_dir))
                _check(results, f"{m.name}: the code calls only what the manifest declares", not missing,
                       "declare or remove: " + ", ".join(str(c) for c in missing[:12]) if missing else "")
        if m.config_schema:
            try:
                import jsonschema

                jsonschema.Draft202012Validator.check_schema(dict(m.config_schema))
                _check(results, f"{m.name}: config schema is valid JSON Schema", True)
            except Exception as exc:  # noqa: BLE001
                _check(results, f"{m.name}: config schema is valid JSON Schema", False, str(exc).splitlines()[0])
    ok = bool(found) and all(r["ok"] for r in results)
    return {"project": str(root), "ok": ok, "captains": [m.name for m in found], "checks": results}
