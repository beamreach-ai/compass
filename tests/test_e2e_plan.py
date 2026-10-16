"""Which end-to-end tests a change runs in CI (.github/scripts/e2e_plan.py)."""

from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("e2e_plan", ROOT / ".github" / "scripts" / "e2e_plan.py")
e2e_plan = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(e2e_plan)


def _files(result):
    return {f.split("/", 1)[1] for shard in result["matrix"]["include"] for f in shard["files"].split()}


def _shards(result):
    return [shard["shard"] for shard in result["matrix"]["include"]]


def test_every_end_to_end_file_is_in_exactly_one_shard():
    on_disk = sorted(p.name for p in (ROOT / "e2e").glob("test_*.py"))
    in_shards = sorted(f for spec in e2e_plan.SHARDS.values() for f in spec["files"])
    assert in_shards == on_disk


def test_the_full_suite_runs_every_shard_and_the_update():
    full = e2e_plan.plan(None)
    assert _shards(full) == ["A", "B", "C", "D"] and full["compose_update"] is True


def test_docs_the_cli_and_unit_tests_run_nothing():
    quiet = e2e_plan.plan(["docs/architecture.md", "cli/src/tools/status.ts", "tests/compass_core/test_core_cli.py", "CONTRIBUTING.md"])
    assert quiet["run"] is False and quiet["matrix"]["include"] == []


def test_what_the_suite_reads_is_not_skipped_as_docs():
    assert e2e_plan.plan(["docs/quickstart.md"])["run"] is True, "REL-3 checks its commands"
    assert e2e_plan.plan(["tests/e2e/update_compose.sh"])["compose_update"] is True


def test_one_captain_runs_its_tests_and_the_smoke_tests():
    decoys = e2e_plan.plan(["captains/honeypot/terraform_generator.py"])
    assert _files(decoys) == {"test_06_decoys.py", "test_00_install.py", "test_02_shell.py"} and _shards(decoys) == ["A", "C"]
    prs = e2e_plan.plan(["captains/prs/sync.py"])
    assert {"test_07_iac.py", "test_08_prs.py"} <= _files(prs), "the watcher needs the pull request IAC-2 opens"
    assert decoys["compose_update"] is False


def test_shared_code_runs_everything():
    for path in ("compass_core/queue.py", "compass_api/app.py", "dashboard/app/components/Sidebar.tsx", "captains/mapper/aws_mapper.py",
                 "e2e/conftest.py", "e2e/fakes/app.py", "docker/Dockerfile"):
        result = e2e_plan.plan([path])
        assert _shards(result) == ["A", "B", "C", "D"], path
    assert e2e_plan.plan(["compass_core/queue.py"])["compose_update"] is False
    assert e2e_plan.plan(["docker/updater/updater.sh"])["compose_update"] is True


def test_a_changed_test_file_runs_that_file_and_what_it_builds_on():
    assert _files(e2e_plan.plan(["e2e/test_08_prs.py"])) == {"test_07_iac.py", "test_08_prs.py"}
    assert _files(e2e_plan.plan(["e2e/test_02_shell.py", "docs/e2e.md"])) == {"test_02_shell.py"}


def test_a_mix_with_one_shared_path_runs_everything():
    assert _shards(e2e_plan.plan(["captains/honeypot/arm.py", "compass_core/config.py"])) == ["A", "B", "C", "D"]
