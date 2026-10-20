"""The action registry (docs/specs/access-model.md §8): what a captain may propose is
declared in its manifest, registered by its actions module, and shown before install."""

from __future__ import annotations

import importlib

import pytest

from compass_core import actions, captains, permissions
from compass_core.actions import Action


def test_the_decoy_actions_are_registered_and_declared():
    importlib.import_module("captains.honeypot.actions")  # registers on import

    assert actions.registered("honeypot") == ["ecs_restart_task", "iam_deactivate_key"]
    assert actions.captain_of("iam_deactivate_key") == "honeypot" and actions.captain_of("nope") is None
    assert actions.get_action("iam_deactivate_key").action_type == "iam_deactivate_key"
    with pytest.raises(KeyError, match="Unknown action type"):
        actions.get_action("nope")
    captains.discover(refresh=True)
    manifest = captains.manifest("honeypot")
    assert set(manifest.permissions.aws.actions) == set(actions.registered("honeypot"))
    shown = permissions.describe(manifest.permissions)
    assert [a["action_type"] for a in shown["actions"]] == ["iam_deactivate_key", "ecs_restart_task"]
    assert shown["actions"][0]["registered"] and shown["actions"][0]["captain"] == "honeypot" and shown["actions"][0]["change_type"]
    assert all(e["captain"] == "honeypot" for e in actions.catalog("honeypot"))


def test_a_type_belongs_to_one_captain():
    class Freeze(Action):
        action_type = "freeze"

    class Other(Action):
        action_type = "freeze"

    actions.register("x", {"freeze": Freeze})
    actions.register("x", {"freeze": Freeze})  # the same class again: fine
    try:
        with pytest.raises(ValueError, match="already registered by x"):
            actions.register("y", {"freeze": Other})
        assert permissions.describe(permissions.Permissions(aws=permissions.AwsPermissions(actions=("freeze", "ghost"))))["actions"] == [
            {"action_type": "freeze", "title": "", "description": "", "change_type": "", "rollback_supported": False, "rollback_automatic": False,
             "scope_fields": [], "captain": "x", "registered": True},
            {"action_type": "ghost", "registered": False},
        ]
    finally:
        actions._registry.pop("freeze", None)


def test_validate_refuses_an_undeclared_or_unregistered_action(tmp_path):
    from compass_core.captain_validate import validate

    pkg = tmp_path / "captains" / "ghost"
    pkg.mkdir(parents=True)
    (pkg / "manifest.py").write_text(
        "from compass_core.captains import AwsPermissions, CaptainManifest, Permissions\n"
        "MANIFEST = CaptainManifest(name='ghost', label='Ghost', summary='s', tier='open', feature='ghost', commands='captains.ghost.commands',\n"
        "    permissions=Permissions(aws=AwsPermissions(actions=('ghost.freeze',))), requires_core='>=0.1', version='0.1.0')\n"
    )
    (pkg / "commands.py").write_text("from compass_core import dispatch\nCOMMANDS = {'hi': lambda p: {}}\ndispatch.register('ghost', COMMANDS)\n")
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "ghost"\nversion = "0.1.0"\n[project.entry-points."compass.captains"]\nghost = "captains.ghost.manifest:MANIFEST"\n')
    from compass_core import dispatch

    try:
        report = validate(str(tmp_path))
    finally:
        dispatch._registry.pop(("ghost", "hi"), None)  # validate registered the sample's command; the registry is process-wide
    failed = {c["check"]: c["detail"] for c in report["checks"] if not c["ok"]}
    assert "ghost: declared action types are registered" in failed and "ghost.freeze" in failed["ghost: declared action types are registered"]


def test_a_catalog_entry_carries_the_block_and_is_held_to_the_same_rules():
    from compass_core import catalog

    good = {"name": "ghost", "label": "Ghost", "summary": "s", "tier": "open", "version": "0.1.0", "requires_core": ">=0.1", "homepage": "https://x",
            "permissions": {"reads": {"ecs": ["Describe*", "List*"]}, "actions": [{"action_type": "ghost.freeze", "title": "Freeze"}],
                            "github": ["contents:read"], "egress": ["api.github.com"]}}
    bad = {**good, "name": "ghoul", "permissions": {"reads": {"iam": ["UpdateAccessKey"]}}}
    parsed = catalog.parse({"schema": 1, "captains": [good, bad]}, source="test")
    assert [e.name for e in parsed.entries] == ["ghost"] and parsed.entries[0].to_dict()["permissions"] == good["permissions"]
    assert "iam:UpdateAccessKey is not a read-only verb" in parsed.rejected["ghoul"]
    assert catalog.permissions_block_errors("nope") == ["must be an object"]
