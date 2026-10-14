"""Publishing a pull request through the GitHub Contents API, once.

The IaC and honeypot writers both do the same four things: make sure a branch
exists off the base, write files through the Contents API (update when the
file is there, create when it is not), and open a pull request for the branch
or refresh the one already open. GitHub refuses a second pull request for a
head that already has one, so "find, then edit or create" is the only correct
shape; a writer that calls `create_pull` blind fails on its second run.

`gh_repo` is a PyGithub repository. A deployment's own publisher pushes
from a real checkout and talks to the REST API with `requests`; it does not use
this module (open_source_split_spec.md §5 S4, S7).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Mapping, Optional

from github import GithubException

logger = logging.getLogger("github-prs")


@dataclass(frozen=True)
class PublishedPR:
    url: str
    branch: str
    base_branch: str
    title: str
    number: Optional[int] = None
    updated: bool = False


def ensure_branch(gh_repo: Any, branch: str, base_branch: str) -> str:
    """Create `branch` from the head of `base_branch`; an existing branch is reused. Returns the base sha."""
    base_sha = gh_repo.get_branch(base_branch).commit.sha
    try:
        gh_repo.create_git_ref(f"refs/heads/{branch}", base_sha)
        logger.info("Created branch %s from %s (%s)", branch, base_branch, str(base_sha)[:8])
    except GithubException as exc:
        if exc.status != 422:
            raise
        logger.info("Branch %s already exists, reusing", branch)
    return base_sha


def commit_files(
    gh_repo: Any,
    branch: str,
    files: Mapping[str, str],
    *,
    add_message: str,
    update_message: str,
) -> None:
    """Write each file on `branch` through the Contents API. `{path}` in a message is the file path."""
    for path, content in files.items():
        try:
            existing = gh_repo.get_contents(path, ref=branch)
            gh_repo.update_file(path, update_message.replace("{path}", path), content, existing.sha, branch=branch)
            logger.info("Updated %s on branch %s", path, branch)
        except GithubException as exc:
            if exc.status != 404:
                raise
            gh_repo.create_file(path, add_message.replace("{path}", path), content, branch=branch)
            logger.info("Created %s on branch %s", path, branch)


def find_open_pr(gh_repo: Any, branch: str) -> Optional[Any]:
    """The open PR whose head is `branch`, if there is one."""
    try:
        owner = gh_repo.owner.login
    except Exception:  # pragma: no cover - defensive; owner is always present in practice
        return None
    for pr in gh_repo.get_pulls(state="open", head=f"{owner}:{branch}"):
        return pr
    return None


def open_or_update_pr(gh_repo: Any, *, branch: str, base_branch: str, title: str, body: str) -> PublishedPR:
    """Open a PR for `branch`, or refresh the title and body of the one already open."""
    existing = find_open_pr(gh_repo, branch)
    if existing:
        existing.edit(title=title, body=body)
        logger.info("Updated existing PR #%s: %s", getattr(existing, "number", "?"), existing.html_url)
        return PublishedPR(
            url=existing.html_url,
            branch=branch,
            base_branch=base_branch,
            title=title,
            number=getattr(existing, "number", None),
            updated=True,
        )
    pr = gh_repo.create_pull(title=title, body=body, head=branch, base=base_branch)
    logger.info("Created PR #%s: %s", getattr(pr, "number", "?"), pr.html_url)
    return PublishedPR(url=pr.html_url, branch=branch, base_branch=base_branch, title=title, number=getattr(pr, "number", None))
