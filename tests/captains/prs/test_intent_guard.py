from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from captains.prs import intent_guard
from captains.prs.intent import intent_for_honeypot, intent_for_iac_import, intent_from_contract


def _run(cmd, cwd):
    subprocess.run(cmd, cwd=str(cwd), check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A repo with main, a remote-tracking origin/main and a PR branch that adds a bucket policy."""
    root = tmp_path / "repo"
    root.mkdir()
    _run(["git", "init", "-b", "main"], root)
    _run(["git", "config", "user.email", "t@example.com"], root)
    _run(["git", "config", "user.name", "t"], root)
    (root / "modules").mkdir()
    (root / "modules" / "storage.tf").write_text('resource "aws_s3_bucket" "logs" {\n  bucket = "logs"\n}\n')
    (root / "modules" / "iam.tf").write_text('resource "aws_iam_role" "app" {\n  name = "app"\n}\n')
    _run(["git", "add", "."], root)
    _run(["git", "commit", "-m", "base"], root)
    _run(["git", "update-ref", "refs/remotes/origin/main", "HEAD"], root)
    _run(["git", "checkout", "-b", "beamreach/f1"], root)
    (root / "modules" / "storage.tf").write_text(
        'resource "aws_s3_bucket" "logs" {\n  bucket = "logs"\n}\n\nresource "aws_s3_bucket_policy" "logs" {\n  bucket = aws_s3_bucket.logs.id\n  policy = "{}"\n}\n'
    )
    _run(["git", "commit", "-am", "fix"], root)
    return root


def _exec_state(changed, validation=None):
    return {"status": "completed", "changed_files": changed, "validation": validation or []}


def test_engine_intent_passes_when_fix_kept(repo):
    intent = intent_from_contract({"intent": {"goal": "g"}, "patch_spec": {"summary": "s"}}, ["modules/storage.tf"], risk=2)
    (repo / "modules" / "storage.tf").write_text((repo / "modules" / "storage.tf").read_text().replace('"{}"', '"{\\"Version\\":\\"2012-10-17\\"}"'))
    result = intent_guard.evaluate(repo, base_ref="origin/main", intent=intent, scope_files=["modules/storage.tf"], requested_files=[], execution_state=_exec_state(["modules/storage.tf"]), final_text="done\nBLOCKERS: none")
    assert result.ok, result.checks
    assert {c["name"] for c in result.checks} >= {"scope", "files_differ_from_base", "verification", "diff_non_empty", "blockers"}


def test_reverting_the_fix_breaks_invariant(repo):
    intent = intent_from_contract({"intent": {"goal": "g"}}, ["modules/storage.tf"])
    (repo / "modules" / "storage.tf").write_text('resource "aws_s3_bucket" "logs" {\n  bucket = "logs"\n}\n')
    result = intent_guard.evaluate(repo, base_ref="origin/main", intent=intent, scope_files=["modules/storage.tf"], requested_files=[], execution_state=_exec_state(["modules/storage.tf"]))
    assert not result.ok and result.reason == "invariant_broken"


def test_scope_expansion_is_refused(repo):
    intent = intent_from_contract({"intent": {"goal": "g"}}, ["modules/storage.tf"])
    (repo / "modules" / "iam.tf").write_text('resource "aws_iam_role" "app" {\n  name = "app2"\n}\n')
    result = intent_guard.evaluate(repo, base_ref="origin/main", intent=intent, scope_files=["modules/storage.tf"], requested_files=[], execution_state=_exec_state(["modules/storage.tf", "modules/iam.tf"]))
    assert result.reason == "scope_expanded"
    ok = intent_guard.evaluate(repo, base_ref="origin/main", intent=intent, scope_files=["modules/storage.tf"], requested_files=["modules/iam.tf"], execution_state=_exec_state(["modules/storage.tf", "modules/iam.tf"]))
    assert ok.ok


def test_validation_errors_and_blockers(repo):
    intent = intent_from_contract({"intent": {"goal": "g"}}, ["modules/storage.tf"])
    bad = intent_guard.evaluate(repo, base_ref="origin/main", intent=intent, scope_files=["modules/storage.tf"], requested_files=[], execution_state=_exec_state(["modules/storage.tf"], [{"dir": "modules", "status": "error", "stderr": "boom"}]))
    assert bad.reason == "verification_failed"
    blocked = intent_guard.evaluate(repo, base_ref="origin/main", intent=intent, scope_files=["modules/storage.tf"], requested_files=[], execution_state=_exec_state(["modules/storage.tf"]), final_text="BLOCKERS: 12: would remove the policy")
    assert blocked.reason == "blocked_by_runtime"
    assert intent_guard.parse_blockers("x\nBLOCKERS: None.") == []
    assert intent_guard.parse_blockers("BLOCKERS: 1: a; 2: b") == ["1: a", "2: b"]


def test_iac_import_invariants(repo):
    (repo / "imports.tf").write_text('import {\n  to = aws_s3_bucket.data\n  id = "data"\n}\n')
    (repo / "resources.tf").write_text('resource "aws_s3_bucket" "data" {\n  bucket = "data"\n}\n')
    intent = intent_for_iac_import(addresses=["aws_s3_bucket.data"], resource_ids=["r1"], files=["imports.tf", "resources.tf"], verified=True, plan_clean=True)
    assert intent["risk"] == 1
    ok = intent_guard.evaluate(repo, base_ref="origin/main", intent=intent, scope_files=["imports.tf", "resources.tf"], requested_files=[], execution_state=_exec_state(["imports.tf", "resources.tf"]))
    assert ok.ok, ok.checks
    (repo / "imports.tf").write_text("")
    broken = intent_guard.evaluate(repo, base_ref="origin/main", intent=intent, scope_files=["imports.tf", "resources.tf"], requested_files=[], execution_state=_exec_state(["imports.tf"]))
    assert broken.reason == "invariant_broken"
    assert any(c["name"] == "import_blocks_present" and not c["ok"] for c in broken.checks)


def test_honeypot_module_invariants(repo):
    (repo / "services.tf").write_text('module "svc_ops_key" {\n  source = "./services/ops-key"\n}\n')
    (repo / "services" / "ops-key").mkdir(parents=True)
    (repo / "services" / "ops-key" / "main.tf").write_text("# module\n")
    plant = intent_for_honeypot("plant", honeytoken={"name": "ops key", "honeytoken_class": "aws_credential", "honeytoken_id": "t1"}, module_name="svc_ops_key", root_file="services.tf", module_dir="services/ops-key", files=["services.tf"])
    assert intent_guard.evaluate(repo, base_ref="origin/main", intent=plant, scope_files=["services.tf", "services/ops-key/main.tf"], requested_files=[], execution_state=_exec_state(["services.tf"])).ok
    (repo / "services.tf").write_text("# nothing\n")
    gone = intent_guard.evaluate(repo, base_ref="origin/main", intent=plant, scope_files=["services.tf"], requested_files=[], execution_state=_exec_state(["services.tf"]))
    assert gone.reason == "invariant_broken"
    decommission = intent_for_honeypot("decommission", honeytoken={"name": "ops key"}, module_name="svc_ops_key", root_file="services.tf", module_dir="services/ops-key", files=["services.tf"])
    still_there = intent_guard.evaluate(repo, base_ref="origin/main", intent=decommission, scope_files=["services.tf"], requested_files=[], execution_state=_exec_state(["services.tf"]))
    assert still_there.reason == "invariant_broken"
