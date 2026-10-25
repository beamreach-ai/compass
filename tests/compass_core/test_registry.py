"""The pull request registry."""

from __future__ import annotations

from compass_core import registry as pr_registry


def test_widen_scope_grows_the_files_the_dirs_the_invariant_and_the_done_criteria_but_not_the_goal():
    pr_registry.reset_memory_for_tests()
    registry = pr_registry.PRRegistry(None, "ws-w")
    intent = {"goal": "Move 1 vulnerable version", "scope": {"files": ["Dockerfile"], "dirs": ["."], "resource_ids": []}, "done_criteria": ["php is at 8.3.36 or later"],
              "invariants": [{"kind": "files_differ_from_base", "files": ["Dockerfile"]}], "verification": {"validate_dirs": ["."], "format_paths": ["Dockerfile"]}}
    registry.record(url="https://github.com/acme/web/pull/9", pr_type="patch_fix", origin={"captain": "patch"}, intent=intent, branch="b", base_branch="main")
    assert registry.update("acme/web", 9, intent={"goal": "something else"})["intent"]["goal"] == "Move 1 vulnerable version", "an intent is never rewritten"
    widened = registry.widen_scope("acme/web", 9, files=["web/composer.json", "web/composer.lock"], done_criteria=["laravel/framework is at 11.2.1 or later"], resource_ids=["td-web"])
    assert widened["intent"]["goal"] == "Move 1 vulnerable version"
    assert widened["intent"]["scope"] == {"files": ["Dockerfile", "web/composer.json", "web/composer.lock"], "dirs": [".", "web"], "resource_ids": ["td-web"]}
    assert widened["intent"]["invariants"] == [{"kind": "files_differ_from_base", "files": ["Dockerfile", "web/composer.json", "web/composer.lock"]}]
    assert widened["intent"]["done_criteria"] == ["php is at 8.3.36 or later", "laravel/framework is at 11.2.1 or later"]
    assert widened["intent"]["verification"]["format_paths"] == ["Dockerfile", "web/composer.json", "web/composer.lock"]
    assert registry.widen_scope("acme/web", 99, files=["x"]) is None
    pr_registry.reset_memory_for_tests()
