"""Tests for branch naming and the create-or-update decision.

Both exist because of a collision failure: branch names were derived from the
first three resource names truncated to 50 characters, so re-importing the same
selection reused a branch that had outlived its (closed) pull request, pushed
new commits onto it, and then died with a 422 because `create_pull` was
unconditional.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from github import GithubException

from captains.iac.github_pr_writer import GitHubPRWriter, _content_branch_suffix
from infra_map.models import ResourceNode


def _node(resource_id: str, name: str) -> ResourceNode:
    return ResourceNode(
        org_id="org-1",
        resource_id=resource_id,
        type="ec2_security_group",
        account_id="123456789012",
        region="us-east-1",
        name=name,
    )


RESOURCES = [_node("sg-aaa", "public-demo-demo-alb"), _node("sg-bbb", "public-demo-demo-tasks")]


class TestContentBranchSuffix:
    def test_is_deterministic(self):
        assert _content_branch_suffix(RESOURCES) == _content_branch_suffix(RESOURCES)

    def test_ignores_ordering(self):
        """The same selection is the same selection regardless of row order."""
        assert _content_branch_suffix(RESOURCES) == _content_branch_suffix(list(reversed(RESOURCES)))

    def test_distinguishes_sets_sharing_a_long_name_prefix(self):
        """The bug the hash exists to kill.

        Truncating a join of the first three names to 50 characters collapsed
        distinct selections onto one branch whenever their leading names shared
        a long enough prefix.
        """
        long_prefix = "public-demo-really-quite-long-resource-name-number-"
        a = [_node("sg-1", long_prefix + "one"), _node("sg-2", long_prefix + "two")]
        b = [_node("sg-3", long_prefix + "one"), _node("sg-4", long_prefix + "three")]

        assert _content_branch_suffix(a) != _content_branch_suffix(b)

    def test_reflects_every_resource_not_just_the_first_three(self):
        four = RESOURCES + [_node("sg-ccc", "extra-one"), _node("sg-ddd", "extra-two")]

        assert _content_branch_suffix(RESOURCES) != _content_branch_suffix(four)

    def test_stays_human_readable(self):
        """A branch list is read by people; keep a resource name visible."""
        suffix = _content_branch_suffix(RESOURCES)

        # sg-aaa sorts before sg-bbb, so the alb name leads regardless of the
        # order the caller passed the resources in.
        assert suffix.startswith("public-demo-demo-alb-")

    def test_handles_an_empty_selection(self):
        assert _content_branch_suffix([]) == "import"


@pytest.fixture
def gh_repo():
    """A repo whose files are all new and which has no open PRs."""
    repo = MagicMock()
    repo.owner.login = "beamreach-ai"
    repo.get_branch.return_value.commit.sha = "a" * 40
    # Every target file is treated as absent, so the writer takes create_file.
    repo.get_contents.side_effect = GithubException(404, {"message": "Not Found"}, {})
    repo.get_pulls.return_value = []
    repo.create_pull.return_value.number = 99
    repo.create_pull.return_value.html_url = "https://github.com/o/r/pull/99"
    return repo


@pytest.fixture
def writer(gh_repo):
    with patch("captains.iac.github_pr_writer.resolve_github_token", return_value="tok"), patch(
        "captains.iac.github_pr_writer.github_client"
    ) as gh:
        gh.return_value.get_repo.return_value = gh_repo
        yield GitHubPRWriter(config={})


HCL = {"environments/demo/beamreach_imports.tf": "import {}\n"}


def test_creates_a_pr_when_none_is_open(writer, gh_repo):
    url = writer.create_pr(repo="o/r", base_branch="main", hcl_files=HCL, resources=RESOURCES)

    assert gh_repo.create_pull.called
    assert url == "https://github.com/o/r/pull/99"


def test_updates_the_existing_pr_instead_of_creating_a_second(writer, gh_repo):
    """Re-importing the same selection must refresh, not 422."""
    open_pr = MagicMock()
    open_pr.number = 42
    open_pr.html_url = "https://github.com/o/r/pull/42"
    gh_repo.get_pulls.return_value = [open_pr]

    url = writer.create_pr(repo="o/r", base_branch="main", hcl_files=HCL, resources=RESOURCES)

    assert not gh_repo.create_pull.called
    assert open_pr.edit.called
    assert url == "https://github.com/o/r/pull/42"


def test_existing_pr_lookup_is_scoped_to_the_branch(writer, gh_repo):
    writer.create_pr(repo="o/r", base_branch="main", hcl_files=HCL, resources=RESOURCES)

    _, kwargs = gh_repo.get_pulls.call_args
    assert kwargs["state"] == "open"
    assert kwargs["head"].startswith("beamreach-ai:beamreach/iac-import/")


def test_updated_pr_body_reflects_the_current_resources(writer, gh_repo):
    open_pr = MagicMock()
    open_pr.number = 42
    open_pr.html_url = "https://github.com/o/r/pull/42"
    gh_repo.get_pulls.return_value = [open_pr]

    writer.create_pr(repo="o/r", base_branch="main", hcl_files=HCL, resources=RESOURCES)

    _, kwargs = open_pr.edit.call_args
    assert "public-demo-demo-alb" in kwargs["title"]
    assert "public-demo-demo-tasks" in kwargs["body"]


def test_reuses_an_existing_branch_rather_than_failing(writer, gh_repo):
    """Branch creation racing an existing ref is expected, not an error."""
    gh_repo.create_git_ref.side_effect = GithubException(422, {"message": "Reference exists"}, {})

    url = writer.create_pr(repo="o/r", base_branch="main", hcl_files=HCL, resources=RESOURCES)

    assert url == "https://github.com/o/r/pull/99"


def test_branch_creation_errors_other_than_conflict_propagate(writer, gh_repo):
    gh_repo.create_git_ref.side_effect = GithubException(403, {"message": "Forbidden"}, {})

    with pytest.raises(GithubException):
        writer.create_pr(repo="o/r", base_branch="main", hcl_files=HCL, resources=RESOURCES)
