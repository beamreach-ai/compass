"""Every captain is described by one manifest, and nothing else spells it out
(docs/captain_extensions_spec.md §1.2, §6.1 O1)."""

from __future__ import annotations

import importlib
import re
from pathlib import Path

import pytest

from compass_core import captains, dispatch

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def manifests():
    captains.discover(refresh=True)
    found = captains.manifests(compatible_only=False)
    assert found, "no manifests discovered"
    return found


def test_the_shipped_set_is_discovered_and_valid(manifests):
    names = [m.name for m in manifests]
    assert names[0] == "core", "the core comes first"
    assert {"core", "mapper", "librarian", "honeypot", "iac", "prs"} <= set(names)
    assert captains.incompatible() == {}
    assert captains.broken() == {}
    for m in manifests:
        assert m.errors() == [], m.name


def test_names_tab_keys_and_route_prefixes_are_unique(manifests):
    names = [m.name for m in manifests]
    assert len(names) == len(set(names))
    tab_keys = [t.key for m in manifests for t in m.tabs]
    assert len(tab_keys) == len(set(tab_keys)), tab_keys
    prefixes = [p for m in manifests for p in m.route_prefixes]
    assert len(prefixes) == len(set(prefixes)), prefixes


def test_every_commands_module_imports_and_registers_its_captain(manifests):
    for m in manifests:
        if not m.commands:
            continue
        importlib.import_module(m.commands)
        assert any(c == m.name for c, _ in dispatch.commands()), f"{m.commands} registers no {m.name} command"


def test_every_dispatchable_command_belongs_to_a_manifest(manifests):
    captains.load_all_commands()
    known = {m.name for m in manifests}
    orphans = {c for c, _ in dispatch.commands() if c not in known and c not in {"x", "fake"}}
    assert not orphans, orphans


def test_every_router_and_service_reference_resolves(manifests):
    for m in manifests:
        for ref in m.routers:
            router = captains.load_reference(ref)
            assert hasattr(router, "routes"), ref
        for spec in m.services:
            assert callable(captains.load_reference(spec.start)), spec.start


def test_dependencies_name_known_captains(manifests):
    names = {m.name for m in manifests}
    for m in manifests:
        for dep in m.depends_on:
            assert dep in names, f"{m.name} depends on unknown {dep}"


def test_config_schemas_are_valid_json_schema(manifests):
    import jsonschema

    for m in manifests:
        if m.config_schema:
            jsonschema.Draft202012Validator.check_schema(dict(m.config_schema))


def test_the_core_cannot_be_turned_off(manifests):
    config = {"captains": {"core": {"enabled": False}}}
    assert captains.states(config)["core"].active


def test_route_prefixes_cover_the_routers_paths(manifests):
    """Every path a captain's router serves is under one of its route prefixes,
    so the active check reaches it."""
    for m in manifests:
        if m.is_core:
            continue
        for ref in m.routers:
            router = captains.load_reference(ref)
            for route in router.routes:
                path = getattr(route, "path", "")
                owner = captains.captain_for_path(path)
                assert owner is not None, f"{path} ({ref}) belongs to no captain"


_NAME_SPELLINGS = {
    "honeypot": re.compile(r'["\']honeypot["\']'),
    "iac": re.compile(r'["\']iac["\']'),
    "prs": re.compile(r'["\']prs["\']'),
    "mapper": re.compile(r'["\']mapper["\']'),
}
# Where a captain may still be named outside its own package: tests, its manifest,
# the registry's shipped list, the legacy feature aliases, and command modules that
# enqueue one another's jobs (a follow-up job names the captain it is for).
_ALLOWED = (
    "tests/", "compass_core/captains.py", "compass_core/features.py", "compass_core/keys.py",
    "compass_core/legacy_honeytoken_ids.py", "compass_core/github_prs.py", "compass_core/aws.py",
    "compass_core/registry.py", "compass_core/policy.py", "compass_core/commands.py", "compass_core/manifest.py",
    "compass_api/routes/", "compass_api/infra_scan.py", "compass_api/app.py", "infra_map/",
)


def test_no_open_core_module_spells_a_captain_out():
    """The open core (compass_core, compass_api outside the routers) reaches captains
    through the registry. The routers and the command modules still name the jobs
    they enqueue; that is the job's name, not a table of captains."""
    offenders = []
    for path in list((ROOT / "compass_core").glob("*.py")) + list((ROOT / "compass_api").rglob("*.py")):
        rel = str(path.relative_to(ROOT))
        if any(rel.startswith(a) for a in _ALLOWED):
            continue
        text = path.read_text()
        for name, pattern in _NAME_SPELLINGS.items():
            if pattern.search(text):
                offenders.append(f"{rel}: {name}")
    assert not offenders, offenders
