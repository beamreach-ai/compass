"""The fix pull request (docs/specs/patching-captain.md §3.5, §3.7, §6): one per repository with the
golden diff, the body, the labels and the watcher's record; regenerated when new findings land;
max_open_prs; the watcher's merge and close moving the findings; a closed pull request's cooldown."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

import boto3
import pytest
from github import GithubException
from moto import mock_aws

from captains.patch import inventory as _inventory
from captains.patch import pr as _pr
from captains.patch import scheduler
from captains.patch.findings import FindingStore
from captains.patch.store import AdvisoryStore
from compass_core import registry as pr_registry
from compass_core.keys import workspace_pk
from tests.captains.patch.test_match import laravel, php2shell, php_shop

PK = workspace_pk("ws-a")
NOW = datetime(2026, 10, 22, 15, 0, tzinfo=timezone.utc)
CONFIG = {"github": {"default_branch": "main"}, "patch": {}}
FILES = {
    ("acme/web", "Dockerfile"): "FROM php:8.3.30-fpm\nRUN apt-get update && apt-get install -y libzip-dev\n",
    ("acme/web", "composer.json"): '{\n  "require": {\n    "laravel/framework": "^11.2.0"\n  }\n}\n',
    ("acme/legacy", "Dockerfile"): "FROM ubuntu:24.04\nRUN apt-get install -y php8.3-fpm=8.3.6-0ubuntu0.24.04.12 nginx\n",
}


class FakeGitHub:
    """Repositories with files on main, branches, pull requests, labels: what the opener touches."""

    def __init__(self):
        self.files = dict(FILES)
        self.branches: dict = {}
        self.refs: dict = {}
        self.pulls: dict = {}
        self.commits: list = []
        self.labels: dict = {}
        self.next_number = 7

    def _blob(self, text: str) -> MagicMock:
        blob = MagicMock()
        blob.decoded_content = text.encode()
        blob.sha = "s"
        return blob

    def repo(self, full_name: str) -> MagicMock:
        gh = MagicMock()
        gh.full_name = full_name
        gh.owner.login = full_name.split("/")[0]
        gh.default_branch = "main"
        gh.get_branch.return_value.commit.sha = "base" * 10

        def get_contents(path, ref="main"):
            if ref != "main":
                if (full_name, ref, path) in self.branches:
                    return self._blob(self.branches[(full_name, ref, path)])
                raise GithubException(404, {"message": "Not Found"}, {})
            if (full_name, path) not in self.files:
                raise GithubException(404, {"message": "Not Found"}, {})
            return self._blob(self.files[(full_name, path)])

        def create_git_ref(ref, sha):
            if ref in self.refs.setdefault(full_name, set()):
                raise GithubException(422, {"message": "Reference already exists"}, {})
            self.refs[full_name].add(ref)

        def write_file(path, message, content, *args, branch=None):
            self.branches[(full_name, branch, path)] = content
            self.commits.append((full_name, branch, path, message))

        def get_pulls(state="open", head=None):
            return [p for (name, _), p in self.pulls.items() if name == full_name and p.state == "open" and (head is None or head.endswith(":" + p.head_branch))]

        def create_pull(title, body, head, base):
            number = self.next_number
            self.next_number += 1
            pull = MagicMock()
            pull.number = number
            pull.html_url = f"https://github.com/{full_name}/pull/{number}"
            pull.title = title
            pull.body = body
            pull.state = "open"
            pull.head_branch = head

            def edit(title=None, body=None):
                if title:
                    pull.title = title
                if body:
                    pull.body = body

            pull.edit = edit
            pull.add_to_labels = lambda *labels: self.labels.setdefault((full_name, number), []).extend(labels)
            self.pulls[(full_name, number)] = pull
            return pull

        gh.get_contents = get_contents
        gh.create_git_ref = create_git_ref
        gh.update_file = write_file
        gh.create_file = write_file
        gh.get_pulls = get_pulls
        gh.create_pull = create_pull
        gh.get_pull = lambda number: self.pulls[(full_name, number)]
        return gh


class Model:
    def call(self, messages, **kw):
        assert "Do not invent versions" in messages[0]["content"]
        return "php2shell lets a crafted multipart request exhaust PHP's memory limit; what it leaves behind runs code before any login. web:41 serves it to the internet."


@pytest.fixture
def world(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    pr_registry.reset_memory_for_tests()
    with mock_aws():
        table = boto3.resource("dynamodb", region_name="us-east-1").create_table(
            TableName="items", KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}, {"AttributeName": "sk", "AttributeType": "S"}], BillingMode="PAY_PER_REQUEST")
        advisories, findings = AdvisoryStore(table, PK), FindingStore(table, PK)
        advisories.put(php2shell())
        github = FakeGitHub()
        registry = pr_registry.PRRegistry(None, "ws-a")
        opener = _pr.PullRequestOpener(CONFIG, workspace_id="ws-a", findings=findings, advisories=advisories, github_client=github.repo, registry=registry, model=Model(), now=NOW)
        yield advisories, findings, github, registry, opener
    pr_registry.reset_memory_for_tests()


def tick(advisories, findings, opener, nodes, edges, **kw):
    return scheduler.tick(CONFIG, workspace_id="ws-a", inventory=_inventory.collect(nodes, edges), advisories=advisories, findings=findings, opener=opener, **kw)


def test_php2shell_opens_one_pull_request_per_repository_with_the_golden_diff(world):
    advisories, findings, github, registry, opener = world
    nodes, edges = php_shop()
    report = tick(advisories, findings, opener, nodes, edges, now_ms=1000)
    opened = {r["repo"]: r for r in report["prs"]["opened"]}
    assert set(opened) == {"acme/web", "acme/legacy"} and report["prs"]["errors"] == {}
    assert "acme/functions" in report["prs"]["skipped"] or "acme/functions" not in opened, "the Bref finding waits in the inbox"
    web = opened["acme/web"]
    assert web["branch"] == "beamreach/patch/20261022-" + __import__("hashlib").sha1(b"acme/web").hexdigest()[:6] and web["files"] == ["Dockerfile"] and web["findings"] == 1
    assert github.branches[("acme/web", web["branch"], "Dockerfile")] == "FROM php:8.3.36-fpm\nRUN apt-get update && apt-get install -y libzip-dev\n"
    assert github.branches[("acme/legacy", opened["acme/legacy"]["branch"], "Dockerfile")] == "FROM ubuntu:24.04\nRUN apt-get install -y php8.3-fpm=8.3.6-0ubuntu0.24.04.13 nginx\n"
    pull = github.pulls[("acme/web", web["number"])]
    assert pull.title == "Security: CVE-2026-109618 — php"
    assert "php2shell lets a crafted multipart request" in pull.body and "| [CVE-2026-109618](https://nvd.nist.gov/vuln/detail/CVE-2026-109618) CVSS 9.2 | php | 8.3.30 → 8.3.36 | web:41 (internet-facing) |" in pull.body
    assert "`Dockerfile`: FROM php:8.3.30-fpm → php:8.3.36-fpm" in pull.body and "Revert this pull request" in pull.body and "Canary: not configured" in pull.body
    assert github.labels[("acme/web", web["number"])] == ["compass:patch", "security"]
    record = registry.get("acme/web", web["number"])
    assert record["type"] == "patch_fix" and record["origin"]["captain"] == "patch" and record["origin"]["finding_id"] == f"acme/web#{web['number']}"
    assert record["intent"]["scope"]["files"] == ["Dockerfile"] and record["intent"]["invariants"] == [{"kind": "files_differ_from_base", "files": ["Dockerfile"]}]
    rows = {r["node_id"]: r for r in findings.list()}
    assert rows["f-web"]["state"] == "pr_open" and rows["f-web"]["pr"]["number"] == web["number"] and rows["f-legacy"]["state"] == "pr_open"
    assert rows["fn-0"]["state"] == "open", "no strategy for a Bref layer whose fixed release is unknown"
    # the second tick changes nothing: the same pull request, refreshed, no new commit
    commits = len(github.commits)
    again = tick(advisories, findings, opener, nodes, edges, now_ms=2000)
    assert again["prs"]["opened"] == [] and {r["repo"] for r in again["prs"]["updated"]} == {"acme/web", "acme/legacy"} and len(github.pulls) == 2
    assert len(github.commits) == commits + 2, "the files are rewritten from the base; the content is the same"


def test_a_new_finding_lands_on_the_open_pull_request_and_kev_adds_its_label(world):
    advisories, findings, github, registry, opener = world
    nodes, edges = php_shop()
    tick(advisories, findings, opener, nodes, edges, now_ms=1000)
    advisories.put(laravel())
    kev = php2shell(kev=True)
    advisories.put(kev)
    report = tick(advisories, findings, opener, nodes, edges, now_ms=2000)
    web = next(r for r in report["prs"]["updated"] if r["repo"] == "acme/web")
    assert web["findings"] == 2 and web["files"] == ["Dockerfile", "composer.json"] and web["updated"] is True
    assert '"laravel/framework": "^11.2.1"' in github.branches[("acme/web", web["branch"], "composer.json")]
    pull = github.pulls[("acme/web", web["number"])]
    assert pull.title.startswith("Security: CVE-2026-109618, CVE-2026-20001") and "exploited in the wild (CISA KEV)" in pull.body
    assert "compass:exploited" in github.labels[("acme/web", web["number"])]
    assert len(github.pulls) == 2, "one pull request per repository"


def test_merge_moves_the_findings_and_the_fix_on_the_map_deploys_them_close_sends_them_back_with_a_cooldown(world):
    advisories, findings, github, registry, opener = world
    nodes, edges = php_shop()
    report = tick(advisories, findings, opener, nodes, edges, now_ms=1000)
    web = next(r for r in report["prs"]["opened"] if r["repo"] == "acme/web")
    legacy = next(r for r in report["prs"]["opened"] if r["repo"] == "acme/legacy")
    moved = _pr.on_pr_status(f"acme/web#{web['number']}", "Resolved", config=CONFIG, workspace_id="ws-a", findings=findings, now_ms=1500)
    assert len(moved["moved"]) == 1 and findings.get(moved["moved"][0])["state"] == "merged"
    fixed_nodes, fixed_edges = php_shop(web_tag="8.3.36-fpm")
    after = tick(advisories, findings, opener, fixed_nodes, fixed_edges, now_ms=2000)
    assert after["changes"]["deployed"] == 1 and next(r for r in findings.list() if r["node_id"] == "f-web")["state"] == "deployed"
    github.pulls[("acme/legacy", legacy["number"])].state = "closed"
    closed = _pr.on_pr_status(f"acme/legacy#{legacy['number']}", "Review declined", config=CONFIG, workspace_id="ws-a", findings=findings, now_ms=2500)
    row = findings.get(closed["moved"][0])
    assert row["state"] == "open" and row["pr"]["cooldown_until"] == 2500 + 14 * 86_400_000 and row["pr"]["closed_by"] == "Review declined"
    quiet = tick(advisories, findings, opener, fixed_nodes, fixed_edges, now_ms=3000)
    assert quiet["prs"]["opened"] == [] and quiet["prs"]["updated"] == [], "no new pull request for fourteen days"
    later = tick(advisories, findings, opener, fixed_nodes, fixed_edges, now_ms=2500 + 15 * 86_400_000)
    assert [r["repo"] for r in later["prs"]["opened"]] == ["acme/legacy"]


def test_max_open_prs_and_the_repository_allow_list_bound_what_opens(world):
    advisories, findings, github, registry, opener = world
    nodes, edges = php_shop()
    opener.config = {**CONFIG, "patch": {"max_open_prs": 1}}
    report = tick(advisories, findings, opener, nodes, edges, now_ms=1000)
    assert [r["repo"] for r in report["prs"]["opened"]] == ["acme/web"], "the exposed repository first"
    assert report["prs"]["skipped"] == {"acme/legacy": "max_open_prs (1) reached"}
    opener.config = {**CONFIG, "patch": {"repos": ["acme/legacy"]}}
    assert _pr.pending(findings.list(), opener.config).keys() == {"acme/legacy"}
    assert _pr.allowed_repo("acme/web", {"prs": {"watcher": {"repos": ["acme/legacy"]}}}) is False


def test_the_body_stands_without_a_model_and_github_errors_are_reported_per_repository(world):
    advisories, findings, github, registry, opener = world
    opener.model = None
    nodes, edges = php_shop()
    original = github.repo

    def repo(full_name):
        if full_name == "acme/legacy":
            raise RuntimeError("no GitHub write access for acme/legacy")
        return original(full_name)

    opener._github_client = repo
    report = tick(advisories, findings, opener, nodes, edges, now_ms=1000)
    assert [r["repo"] for r in report["prs"]["opened"]] == ["acme/web"] and report["prs"]["errors"] == {"acme/legacy": "RuntimeError: no GitHub write access for acme/legacy"}
    body = github.pulls[("acme/web", 7)].body
    assert body.startswith("Compass found 1 vulnerable version in this repository") and "## Findings" in body
    assert next(r for r in findings.list() if r["node_id"] == "f-legacy")["state"] == "open"
