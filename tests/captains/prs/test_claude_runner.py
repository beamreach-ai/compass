"""The open runner against a scripted tool-runner loop and a local bare origin: no network, no key."""

from __future__ import annotations

import json
import logging
import os
import stat
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from captains.prs import claude_runner, revision
from captains.prs.claude_runner import ClaudeRevisionRunner
from captains.prs.revision import RevisionState, build_contract
from compass_core import anthropic_model

LOG = logging.getLogger("test.claude_runner")
PR_FILE = "modules/storage.tf"
FIXED = 'resource "aws_s3_bucket" "logs" {\n  bucket = "logs"\n}\n\nresource "aws_s3_bucket_policy" "logs" {\n  bucket = aws_s3_bucket.logs.id\n  policy = "{}"\n}\n'
REVISED = FIXED.replace('policy = "{}"', "policy = data.aws_iam_policy_document.logs.json")


def _run(cmd, cwd):
    return subprocess.run(cmd, cwd=str(cwd), check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True).stdout


def _head(bare: Path, branch: str) -> str:
    return _run(["git", "--git-dir", str(bare), "rev-parse", branch], bare).strip()


@pytest.fixture(autouse=True)
def no_terraform(monkeypatch):
    monkeypatch.setattr(claude_runner, "_terraform_bin", lambda: None)


@pytest.fixture
def origin(tmp_path: Path) -> Path:
    """A bare repo whose main has storage.tf and whose beamreach/f1 adds a bucket policy."""
    bare = tmp_path / "origin.git"
    _run(["git", "init", "--bare", "-b", "main", str(bare)], tmp_path)
    seed = tmp_path / "seed"
    _run(["git", "clone", str(bare), str(seed)], tmp_path)
    _run(["git", "config", "user.email", "t@example.com"], seed)
    _run(["git", "config", "user.name", "t"], seed)
    _run(["git", "checkout", "-b", "main"], seed)
    (seed / "modules").mkdir()
    (seed / PR_FILE).write_text('resource "aws_s3_bucket" "logs" {\n  bucket = "logs"\n}\n')
    _run(["git", "add", "."], seed)
    _run(["git", "commit", "-m", "base"], seed)
    _run(["git", "push", "origin", "main"], seed)
    _run(["git", "checkout", "-b", "beamreach/f1"], seed)
    (seed / PR_FILE).write_text(FIXED)
    _run(["git", "commit", "-am", "fix"], seed)
    _run(["git", "push", "origin", "beamreach/f1"], seed)
    return bare


def _clone(origin: Path, dest: Path, branch: str = "beamreach/f1", base: str = "main") -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    _run(["git", "clone", "--branch", branch, str(origin), str(dest)], dest.parent)
    _run(["git", "-C", str(dest), "fetch", "origin", f"+refs/heads/{base}:refs/remotes/origin/{base}"], dest)
    return dest


@pytest.fixture
def checkout(origin: Path, tmp_path: Path) -> Path:
    return _clone(origin, tmp_path / "work")


@pytest.fixture
def local_push(monkeypatch, origin):
    """Push goes to the bare repo; the token comes from nowhere."""
    monkeypatch.setattr(claude_runner, "push_url", lambda repo, token: str(origin))
    monkeypatch.setattr(claude_runner, "resolve_github_token_for_repo", lambda *_a, **_k: "tok")


# ── a scripted SDK ──


def _message(content, stop_reason):
    return SimpleNamespace(role="assistant", content=content, stop_reason=stop_reason,
                           usage=SimpleNamespace(input_tokens=100, output_tokens=20, cache_read_input_tokens=None))


class FakeToolRunner:
    """The SDK loop, scripted: each ("tool", name, input) step runs the tool by name, ("final", text) ends the turn."""

    def __init__(self, params, script):
        self.params = params
        self.script = script
        self.results = []

    def __iter__(self):
        tools = {fn.__name__: fn for fn in self.params["tools"]}
        for step in self.script:
            if step[0] == "tool":
                _, name, kwargs = step
                try:
                    content, is_error = tools[name](**kwargs), False
                except Exception as exc:  # the SDK turns a raised tool error into an is_error result
                    content, is_error = str(exc), True
                self.results.append((name, content, is_error))
                yield _message([SimpleNamespace(type="tool_use", id="t1", name=name, input=kwargs)], "tool_use")
            else:
                yield _message([SimpleNamespace(type="thinking", thinking="…"), SimpleNamespace(type="text", text=step[1])], step[2] if len(step) > 2 else "end_turn")


class FakeClient:
    def __init__(self, script):
        self.script = script
        self.params = None
        self.runner = None
        self.beta = SimpleNamespace(messages=SimpleNamespace(tool_runner=self._tool_runner))

    def _tool_runner(self, **params):
        self.params = params
        self.runner = FakeToolRunner(params, self.script)
        return self.runner


@pytest.fixture
def fake_sdk(monkeypatch):
    monkeypatch.setattr(claude_runner, "_sdk", lambda: (lambda fn: fn, RuntimeError))


def _intent():
    return {
        "goal": "Block public access on the logs bucket", "risk": 2, "scope": {"files": [PR_FILE]},
        "invariants": [{"kind": "files_differ_from_base", "files": [PR_FILE]}, {"kind": "resource_blocks_present", "addresses": ["aws_s3_bucket_policy.logs"]}],
        "verification": {"validate_dirs": [], "format_paths": []},
    }


def _record(intent=None):
    return {
        "repo": "acme/infra", "number": 7, "url": "https://github.com/acme/infra/pull/7", "type": "security_fix",
        "branch": "beamreach/f1", "base_branch": "main", "origin": {"captain": "engine", "finding_id": "f1"},
        "intent": intent or _intent(), "state": "open", "watch": {"status": "watching"}, "counters": {"revisions": 0},
    }


def _feedback(body="use a policy document instead of a literal"):
    return {"trigger": "review", "comments": [{"id": 101, "source": "review_comment", "path": PR_FILE, "line": 6, "body": body, "author": "alice", "kind": "change_in_scope"}],
            "ci_failures": [], "conflicts": [], "requested_files": [PR_FILE]}


def _state(repo_root: Path, intent=None) -> RevisionState:
    """The state run_revision builds, for one PR branch checked out at repo_root."""
    record = _record(intent)
    contract = build_contract(record=record, repo_root=repo_root, scope_files=[PR_FILE], requested_files=[PR_FILE],
                              review_feedback=_feedback(), origin_context={"summary": "finding: public bucket"}, domain="remediation")
    allowed = contract["patch_spec"]["allowed_files"]
    return RevisionState(
        message="Revise pull request acme/infra#7", session_id="pr:acme/infra#7", target="demo",
        context={"branch_name": "beamreach/f1", "pr": {"repo": "acme/infra", "number": 7, "url": record["url"]}, "workspace_id": "ws-1"},
        intent=contract["intent"],
        target_state={"local_repo_path": str(repo_root), "repo": "acme/infra", "branch": "beamreach/f1", "default_branch": "main"},
        patch_state={"mode": "local_repo_patch", "status": "planned", "summary": contract["patch_spec"]["summary"], "repo": "acme/infra",
                     "repo_path": str(repo_root), "branch": "beamreach/f1", "base_branch": "main", "allowed_files": allowed, "allow_all_files": False},
        execution_contract=contract,
    )


GOOD_SCRIPT = [
    ("tool", "list_files", {"glob": "modules/*.tf"}),
    ("tool", "read_file", {"path": PR_FILE}),
    ("tool", "write_file", {"path": "modules/iam.tf", "content": "# new\n"}),
    ("tool", "write_file", {"path": "../outside.tf", "content": "# escape\n"}),
    ("tool", "run", {"cmd": "rm -rf /"}),
    ("tool", "run", {"cmd": "terraform fmt -check", "dir": "modules"}),
    ("tool", "write_file", {"path": PR_FILE, "content": REVISED}),
    ("final", "Replaced the literal policy with a policy document reference.\n\nBLOCKERS: none"),
]


# ── execute ──


def test_execute_writes_only_allowed_files_and_records_the_run(fake_sdk, checkout):
    client = FakeClient(GOOD_SCRIPT)
    runner = ClaudeRevisionRunner(client=client)
    state = runner.execute(_state(checkout), target="demo", config={}, model=None, logger=LOG)

    results = {name: (content, is_error) for name, content, is_error in client.runner.results}
    assert "storage.tf" in results["list_files"][0] and not results["list_files"][1]
    assert results["read_file"][0] == FIXED
    assert "not in patch_spec.allowed_files" in client.runner.results[2][1] and client.runner.results[2][2]
    assert "outside the checkout" in client.runner.results[3][1] and client.runner.results[3][2]
    assert "only `terraform fmt -check`" in client.runner.results[4][1] and client.runner.results[4][2]
    assert "skipped" in client.runner.results[5][1] and not client.runner.results[5][2]
    assert client.runner.results[6] == ("write_file", f"wrote {PR_FILE} ({len(REVISED)} characters)", False)
    assert not (checkout / "modules" / "iam.tf").exists() and not (checkout.parent / "outside.tf").exists()
    assert (checkout / PR_FILE).read_text() == REVISED

    es = state.execution_state
    assert es["status"] == "completed" and es["error"] is None
    assert es["changed_files"] == [PR_FILE] and es["mode"] == "claude_execution" and es["repo_path"] == str(checkout)
    assert es["validation"] == [] and es["format"]["status"] == "skipped"
    rr = state.execution_contract["runtime_result"]
    assert rr is es["runtime"]
    assert rr["final_text"].endswith("BLOCKERS: none") and rr["blockers"] == []
    assert rr["files_written"] == [PR_FILE] and rr["runner"] == "anthropic_tool_runner"
    assert rr["usage"] == {"input_tokens": 800, "output_tokens": 160} and rr["turns"] == 8
    assert state.execution_contract["status"] == "completed"
    assert "storage.tf" in state.answer

    params = client.params
    assert params["model"] == "claude-opus-5-5" and params["thinking"] == {"type": "adaptive"} and params["max_iterations"] == 20
    assert [fn.__name__ for fn in params["tools"]] == ["read_file", "write_file", "list_files", "run"]
    system = params["system"]
    for rule in revision.REVISION_RULES:
        assert rule in system
    brief = json.loads(system.split("```json\n", 1)[1].split("```")[0])
    assert brief["patch_spec"]["allowed_files"] == [PR_FILE] and brief["review_feedback"]["comments"][0]["id"] == 101
    assert brief["origin_context"]["summary"] == "finding: public bucket" and "goal" in brief["intent"]
    assert "acme/infra#7" in params["messages"][0]["content"] and "policy document" in params["messages"][0]["content"]


def test_execute_without_a_final_blockers_line_is_an_error(fake_sdk, checkout):
    state = ClaudeRevisionRunner(client=FakeClient([("tool", "write_file", {"path": PR_FILE, "content": REVISED}), ("final", "done", "max_tokens")])).execute(
        _state(checkout), target="demo", config={}, model=None, logger=LOG)
    assert state.execution_state["status"] == "error" and state.execution_state["error_type"] == "no_final_answer"
    assert "max_tokens" in state.execution_state["error"]


def test_execute_with_no_change_and_no_blockers_is_an_error(fake_sdk, checkout):
    state = ClaudeRevisionRunner(client=FakeClient([("final", "Nothing to do.\nBLOCKERS: none")])).execute(_state(checkout), target="demo", config={}, model=None, logger=LOG)
    assert state.execution_state["status"] == "error" and state.execution_state["error_type"] == "no_diff"


def test_execute_with_blockers_and_no_change_completes_for_the_guard(fake_sdk, checkout):
    state = ClaudeRevisionRunner(client=FakeClient([("final", "BLOCKERS: 101: removing the policy would undo the fix")])).execute(
        _state(checkout), target="demo", config={}, model=None, logger=LOG)
    assert state.execution_state["status"] == "completed" and state.execution_state["changed_files"] == []
    assert state.execution_contract["runtime_result"]["blockers"] == ["101: removing the policy would undo the fix"]


def test_execute_survives_a_runtime_exception(fake_sdk, checkout):
    class Boom(FakeClient):
        def _tool_runner(self, **params):
            raise ConnectionError("api down")

    state = ClaudeRevisionRunner(client=Boom([])).execute(_state(checkout), target="demo", config={}, model=None, logger=LOG)
    assert state.execution_state["status"] == "error" and "api down" in state.execution_state["error"]


def test_execute_without_sdk_or_key_marks_the_runtime_unavailable(monkeypatch, checkout):
    def no_sdk():
        raise ImportError("No module named 'anthropic'")

    monkeypatch.setattr(claude_runner, "_sdk", no_sdk)
    state = ClaudeRevisionRunner().execute(_state(checkout), target="demo", config={}, model=None, logger=LOG)
    assert state.execution_state["error_type"] == "runtime_unavailable" and "not installed" in state.execution_state["error"]

    monkeypatch.setattr(claude_runner, "_sdk", lambda: (lambda fn: fn, RuntimeError))
    monkeypatch.setattr(anthropic_model, "available", lambda: False)
    state = ClaudeRevisionRunner().execute(_state(checkout), target="demo", config={}, model=None, logger=LOG)
    assert state.execution_state["error_type"] == "runtime_unavailable" and "ANTHROPIC_API_KEY" in state.execution_state["error"]


def test_verification_runs_the_contract_checks_and_records_failures(fake_sdk, monkeypatch, checkout, tmp_path):
    fake = tmp_path / "terraform"
    fake.write_text('#!/bin/sh\ncase "$1" in\n  validate) echo "Error: bad reference" >&2; exit 1;;\n  *) exit 0;;\nesac\n')
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setattr(claude_runner, "_terraform_bin", lambda: str(fake))
    intent = {**_intent(), "verification": {"validate_dirs": ["modules"], "format_paths": [PR_FILE]}}
    client = FakeClient([("tool", "run", {"cmd": "terraform validate", "dir": "modules"}), ("tool", "write_file", {"path": PR_FILE, "content": REVISED}), ("final", "Done.\nBLOCKERS: none")])

    state = ClaudeRevisionRunner(client=client).execute(_state(checkout, intent), target="demo", config={}, model=None, logger=LOG)

    assert client.runner.results[0][1].startswith("exit 1") and "bad reference" in client.runner.results[0][1]
    es = state.execution_state
    assert es["status"] == "completed"  # the guard turns a validation error into `verification_failed`
    assert es["format"] == {"status": "completed", "paths": [PR_FILE], "stdout": "", "stderr": ""}
    assert es["validation"] == [{"dir": "modules", "status": "error", "stdout": "", "stderr": "Error: bad reference"}]
    assert es["runtime"]["runs"] == [{"cmd": "terraform validate", "dir": "modules", "returncode": 1}]


# ── publish ──


def test_publish_pushes_one_commit_to_the_pr_branch(fake_sdk, local_push, origin, checkout):
    before_pr, before_main = _head(origin, "beamreach/f1"), _head(origin, "main")
    runner = ClaudeRevisionRunner(client=FakeClient(GOOD_SCRIPT))
    state = runner.execute(_state(checkout), target="demo", config={"github": {"author_email": "bot@compass.test"}}, model=None, logger=LOG)
    (checkout / "notes.txt").write_text("stray\n")  # outside the allowed files: never committed

    state = runner.publish(state, target="demo", config={"github": {"author_email": "bot@compass.test"}}, logger=LOG, workspace_id="ws-1")

    ps = state.publish_state
    assert ps["status"] == "completed" and ps["branch"] == "beamreach/f1" and ps["base_branch"] == "main"
    assert ps["changed_files"] == [PR_FILE] and ps["pr_url"].endswith("/pull/7") and ps["repo"] == "acme/infra"
    assert ps["commit_sha"] == _head(origin, "beamreach/f1") != before_pr
    assert _head(origin, "main") == before_main
    log = _run(["git", "--git-dir", str(origin), "log", "-1", "--format=%s%n%b%n%ae%n%P", "beamreach/f1"], origin).splitlines()
    assert log[0] == "compass: Replaced the literal policy with a policy document reference."
    assert "Revises https://github.com/acme/infra/pull/7" in log[1] and "Co-Authored-By" not in "\n".join(log)
    assert log[-2] == "bot@compass.test" and log[-1] == before_pr
    shown = _run(["git", "--git-dir", str(origin), "show", "--stat", "--format=", "beamreach/f1"], origin)
    assert PR_FILE in shown and "notes.txt" not in shown
    assert "Pushed" in state.answer


def test_publish_with_nothing_to_commit_does_not_push(local_push, origin, checkout):
    before = _head(origin, "beamreach/f1")
    state = ClaudeRevisionRunner().publish(_state(checkout), target="demo", config={}, logger=LOG)
    assert state.publish_state["status"] == "nothing_to_commit" and "nothing to commit" in state.publish_state["error"]
    assert _head(origin, "beamreach/f1") == before


def test_publish_without_a_token_or_on_a_rejected_push_reports_the_step(monkeypatch, local_push, origin, checkout):
    (checkout / PR_FILE).write_text(REVISED)
    monkeypatch.setattr(claude_runner, "resolve_github_token_for_repo", lambda *_a, **_k: None)
    state = ClaudeRevisionRunner().publish(_state(checkout), target="demo", config={}, logger=LOG)
    assert state.publish_state["status"] == "error" and "token" in state.publish_state["error"]

    monkeypatch.setattr(claude_runner, "resolve_github_token_for_repo", lambda *_a, **_k: "tok")
    other = _clone(origin, checkout.parent / "other")
    (other / PR_FILE).write_text(FIXED + "# moved on\n")
    _run(["git", "-c", "user.name=x", "-c", "user.email=x@x", "commit", "-am", "someone else pushed"], other)
    _run(["git", "push", "origin", "beamreach/f1"], other)
    moved = _head(origin, "beamreach/f1")
    state = ClaudeRevisionRunner().publish(_state(checkout), target="demo", config={}, logger=LOG)
    assert state.publish_state["status"] == "error" and state.publish_state["error"].startswith("git push failed")
    assert _head(origin, "beamreach/f1") == moved  # never forced


# ── through run_revision ──


@pytest.fixture
def wired(monkeypatch, origin, tmp_path):
    monkeypatch.setenv("REPO_WORKDIR", str(tmp_path / "workdir"))
    monkeypatch.setattr(revision, "resolve_github_token_for_repo", lambda *_a, **_k: "tok")
    monkeypatch.setattr(revision, "prepare_checkout", lambda dest, *, repo, branch, base_branch, token: _clone(origin, dest, branch, base_branch))
    monkeypatch.setattr(claude_runner, "push_url", lambda repo, token: str(origin))
    monkeypatch.setattr(claude_runner, "resolve_github_token_for_repo", lambda *_a, **_k: "tok")

    def install(script):
        # no script: the runner builds its own client, so it must find a credential first
        monkeypatch.setattr(revision, "revision_runner", lambda: ClaudeRevisionRunner(client=FakeClient(script) if script is not None else None))

    return install


def _revise(trigger="review"):
    return revision.run_revision(record=_record(), review_feedback=_feedback(), origin_context={"summary": "finding: public bucket", "changed_since_pr": [], "notes": []},
                                 config={"github": {}}, target="demo", workspace_id="ws-1", trigger=trigger)


def test_run_revision_pushes_through_the_open_runner(fake_sdk, wired, origin, tmp_path):
    wired(GOOD_SCRIPT)
    result = _revise()
    assert result["outcome"] == "pushed" and result["reason"] is None, result
    assert result["commit_sha"] == _head(origin, "beamreach/f1") and result["changed_files"] == [PR_FILE] and result["blockers"] == []
    assert result["guard"]["ok"] and {c["name"] for c in result["guard"]["checks"]} >= {"scope", "files_differ_from_base", "verification", "diff_non_empty", "blockers"}
    assert not any((tmp_path / "workdir").rglob("*.tf"))  # the checkout is removed afterwards


def test_run_revision_refuses_blockers_without_a_push(fake_sdk, wired, origin):
    before = _head(origin, "beamreach/f1")
    wired([("final", "BLOCKERS: 101: removing the policy would undo the fix")])
    result = _revise()
    assert result["outcome"] == "refused" and result["reason"] == "blocked_by_runtime"
    assert result["blockers"] == ["101: removing the policy would undo the fix"] and _head(origin, "beamreach/f1") == before


def test_run_revision_without_a_credential_is_no_revision_runner(monkeypatch, wired, origin):
    wired(None)
    monkeypatch.setattr(claude_runner, "_sdk", lambda: (lambda fn: fn, RuntimeError))
    monkeypatch.setattr(anthropic_model, "available", lambda: False)
    before = _head(origin, "beamreach/f1")
    result = _revise()
    assert result["outcome"] == "failed" and result["reason"] == "no_revision_runner" and "ANTHROPIC_API_KEY" in result["error"]
    assert _head(origin, "beamreach/f1") == before


# ── the default ──


def test_revision_runner_is_the_open_runner_only_with_a_credential(monkeypatch):
    monkeypatch.setattr(revision, "_runner_factory", None)
    monkeypatch.setattr(anthropic_model, "available", lambda: False)
    assert revision.revision_runner() is None

    monkeypatch.setattr(anthropic_model, "available", lambda: True)
    assert isinstance(revision.revision_runner(), ClaudeRevisionRunner)

    product = object()
    monkeypatch.setattr(revision, "_runner_factory", lambda: product)
    assert revision.revision_runner() is product


def test_available_follows_the_environment(monkeypatch, tmp_path):
    monkeypatch.setattr(revision, "_runner_factory", None)
    monkeypatch.setattr("importlib.util.find_spec", lambda name: object())
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    monkeypatch.setenv("ANTHROPIC_CONFIG_DIR", str(tmp_path / "none"))
    assert revision.revision_runner() is None
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    assert isinstance(revision.revision_runner(), ClaudeRevisionRunner)


# ── the real SDK, when it is installed ──


def test_real_tool_runner_drives_the_workspace_tools(monkeypatch, checkout):
    """The SDK's own BetaToolRunner and @beta_tool, with the HTTP call replaced by scripted replies."""
    anthropic = pytest.importorskip("anthropic")
    from anthropic.types.beta import BetaMessage

    replies = iter([
        {"type": "tool_use", "id": "toolu_1", "name": "write_file", "input": {"path": "modules/iam.tf", "content": "x"}},
        {"type": "tool_use", "id": "toolu_2", "name": "write_file", "input": {"path": PR_FILE, "content": REVISED}},
        {"type": "text", "text": "Referenced the policy document.\nBLOCKERS: none"},
    ])
    seen = []

    def parse(**params):
        seen.append(params)
        block = next(replies)
        stop = "tool_use" if block["type"] == "tool_use" else "end_turn"
        return BetaMessage.model_validate({"id": "msg", "type": "message", "role": "assistant", "model": params["model"], "content": [block], "stop_reason": stop,
                                           "stop_sequence": None, "usage": {"input_tokens": 10, "output_tokens": 5}})

    client = anthropic.Anthropic(api_key="sk-test")
    monkeypatch.setattr(client.beta.messages, "parse", parse)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    state = ClaudeRevisionRunner(client=client).execute(_state(checkout), target="demo", config={}, model=None, logger=LOG)

    assert state.execution_state["status"] == "completed" and state.execution_state["changed_files"] == [PR_FILE]
    assert (checkout / PR_FILE).read_text() == REVISED and not (checkout / "modules" / "iam.tf").exists()
    refused = seen[1]["messages"][2]["content"][0]
    assert refused["type"] == "tool_result" and refused["is_error"] and "not in patch_spec.allowed_files" in str(refused["content"])
    names = [t["name"] for t in seen[0]["tools"]]
    assert names == ["read_file", "write_file", "list_files", "run"]
    assert seen[0]["tools"][1]["input_schema"]["required"] == ["path", "content"]
    assert state.execution_contract["runtime_result"]["usage"] == {"input_tokens": 30, "output_tokens": 15}
    assert os.environ.get("ANTHROPIC_API_KEY") == "sk-test"
