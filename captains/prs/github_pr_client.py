"""The little GitHub the PR watcher needs: read a PR's feedback, reply, re-request review.

Pushes and PR creation are the publisher's job, not this client's. Threads are never
resolved here: resolution is a person's act (see docs/auto_merge_watcher_spec.md §6).
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional

import requests

from compass_core.github import api_url


def graphql_url() -> str:
    """github.com serves GraphQL at /graphql beside the REST API; GitHub Enterprise
    Server at /api/graphql where REST is /api/v3."""
    base = api_url()
    return base[: -len("/v3")] + "/graphql" if base.endswith("/api/v3") else f"{base}/graphql"


COMPASS_MARKER = "<!-- compass:pr-watcher -->"
TRUSTED_ASSOCIATIONS = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})


class GitHubPRClient:
    def __init__(self, token: str, *, timeout: int = 20):
        self._headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "compass",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        self._timeout = timeout

    # ── transport ──────────────────────────────────────────────────────────

    def _request(self, method: str, path: str, *, json_body: Optional[Dict[str, Any]] = None, params: Optional[Dict[str, Any]] = None) -> Any:
        response = requests.request(method, f"{api_url()}{path}", headers=self._headers, json=json_body, params=params, timeout=self._timeout)
        if response.status_code >= 400:
            raise RuntimeError(f"GitHub {method} {path} failed ({response.status_code}): {response.text[:300]}")
        if response.status_code == 204 or not response.content:
            return None
        return response.json()

    def _paginated(self, path: str, *, params: Optional[Dict[str, Any]] = None, limit: int = 300) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        page = 1
        while len(out) < limit:
            batch = self._request("GET", path, params={**(params or {}), "per_page": 100, "page": page})
            if not isinstance(batch, list) or not batch:
                break
            out.extend(batch)
            if len(batch) < 100:
                break
            page += 1
        return out[:limit]

    # ── reads ──────────────────────────────────────────────────────────────

    def get_pull(self, repo: str, number: int) -> Dict[str, Any]:
        return self._request("GET", f"/repos/{repo}/pulls/{int(number)}") or {}

    def list_review_comments(self, repo: str, number: int) -> List[Dict[str, Any]]:
        return self._paginated(f"/repos/{repo}/pulls/{int(number)}/comments")

    def list_issue_comments(self, repo: str, number: int) -> List[Dict[str, Any]]:
        return self._paginated(f"/repos/{repo}/issues/{int(number)}/comments")

    def list_reviews(self, repo: str, number: int) -> List[Dict[str, Any]]:
        return self._paginated(f"/repos/{repo}/pulls/{int(number)}/reviews")

    def list_commits(self, repo: str, number: int) -> List[Dict[str, Any]]:
        return self._paginated(f"/repos/{repo}/pulls/{int(number)}/commits")

    # ── writes ─────────────────────────────────────────────────────────────

    def reply_to_review_comment(self, repo: str, number: int, comment_id: int, body: str) -> Dict[str, Any]:
        return self._request("POST", f"/repos/{repo}/pulls/{int(number)}/comments/{int(comment_id)}/replies", json_body={"body": body}) or {}

    def create_issue_comment(self, repo: str, number: int, body: str) -> Dict[str, Any]:
        return self._request("POST", f"/repos/{repo}/issues/{int(number)}/comments", json_body={"body": body}) or {}

    def request_reviewers(self, repo: str, number: int, logins: Iterable[str]) -> None:
        logins = [login for login in logins if isinstance(login, str) and login.strip()]
        if not logins:
            return
        self._request("POST", f"/repos/{repo}/pulls/{int(number)}/requested_reviewers", json_body={"reviewers": logins})

    # ── state (one GraphQL query per PR) ───────────────────────────────────

    def graphql(self, query: str, variables: Dict[str, Any]) -> Dict[str, Any]:
        response = requests.post(f"{graphql_url()}", headers=self._headers, json={"query": query, "variables": variables}, timeout=self._timeout)
        if response.status_code >= 400:
            raise RuntimeError(f"GitHub GraphQL failed ({response.status_code}): {response.text[:300]}")
        payload = response.json()
        if payload.get("errors"):
            raise RuntimeError(f"GitHub GraphQL errors: {str(payload['errors'])[:300]}")
        return payload.get("data") or {}

    def pull_request_state(self, repo: str, number: int) -> Dict[str, Any]:
        """Everything ``prs.sync`` needs about one PR, normalised (see ``normalize_pull_state``)."""
        owner, name = repo.split("/", 1)
        data = self.graphql(PULL_STATE_QUERY, {"owner": owner, "name": name, "number": int(number)})
        node = ((data.get("repository") or {}).get("pullRequest")) or None
        if not node:
            raise RuntimeError(f"pull request {repo}#{number} not found")
        return normalize_pull_state(node)

    def add_labels(self, repo: str, number: int, labels: Iterable[str]) -> None:
        labels = [l for l in labels if isinstance(l, str) and l.strip()]
        if labels:
            self._request("POST", f"/repos/{repo}/issues/{int(number)}/labels", json_body={"labels": labels})

    def remove_label(self, repo: str, number: int, label: str) -> None:
        try:
            self._request("DELETE", f"/repos/{repo}/issues/{int(number)}/labels/{label}")
        except RuntimeError as exc:
            if "404" not in str(exc):
                raise

    # ── feedback ───────────────────────────────────────────────────────────

    def fetch_open_feedback(self, repo: str, number: int, *, since_ms: Optional[int] = None) -> Dict[str, Any]:
        """Top-level review and issue comments a revision could act on.

        Compass's own comments (marker), bots and replies are dropped. Authors
        outside OWNER/MEMBER/COLLABORATOR are kept but marked ``ignore`` so the
        caller can answer them without acting on them."""
        pull = self.get_pull(repo, number)
        head_sha = ((pull.get("head") or {}).get("sha")) if isinstance(pull, dict) else None
        comments: List[Dict[str, Any]] = []

        def _accept(raw: Dict[str, Any], *, path: Optional[str], line: Optional[int], source: str) -> None:
            body = str(raw.get("body") or "")
            if COMPASS_MARKER in body:
                return
            user = raw.get("user") if isinstance(raw.get("user"), dict) else {}
            if str(user.get("type") or "") == "Bot":
                return
            created = str(raw.get("created_at") or "")
            if since_ms and _iso_to_ms(created) <= since_ms:
                return
            association = str(raw.get("author_association") or "NONE").upper()
            trusted = association in TRUSTED_ASSOCIATIONS
            comments.append({
                "id": raw.get("id"),
                "source": source,
                "path": path,
                "line": line,
                "body": body,
                "author": user.get("login"),
                "author_association": association,
                "created_at": created,
                "kind": "change_in_scope" if trusted else "ignore",
                "reason": None if trusted else "author is not a repository collaborator",
            })

        for raw in self.list_review_comments(repo, number):
            if raw.get("in_reply_to_id"):
                continue
            _accept(raw, path=raw.get("path"), line=raw.get("line") or raw.get("original_line"), source="review_comment")
        for raw in self.list_issue_comments(repo, number):
            _accept(raw, path=None, line=None, source="issue_comment")

        requested_changes_by: List[str] = []
        for review in self.list_reviews(repo, number):
            if str(review.get("state") or "").upper() == "CHANGES_REQUESTED":
                login = ((review.get("user") or {}).get("login")) if isinstance(review.get("user"), dict) else None
                if login and login not in requested_changes_by:
                    requested_changes_by.append(login)
        comments.sort(key=lambda c: c.get("created_at") or "")
        return {"comments": comments, "requested_changes_by": requested_changes_by, "head_sha": head_sha}


def _iso_to_ms(value: str) -> int:
    from datetime import datetime, timezone

    try:
        return int(datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp() * 1000)
    except Exception:
        return 0



PULL_STATE_QUERY = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      state merged mergedAt closedAt updatedAt title isDraft
      headRefOid headRefName baseRefName mergeable mergeStateStatus reviewDecision
      autoMergeRequest { enabledAt mergeMethod }
      labels(first: 20) { nodes { name } }
      commits(last: 10) { nodes { commit { oid committedDate author { name email user { login } } committer { name email user { login } } } } }
      latestReviews(first: 20) { nodes { state submittedAt author { login __typename } } }
      reviewThreads(first: 50) {
        nodes { isResolved resolvedBy { login } comments(first: 1) { nodes { databaseId body path line originalLine createdAt authorAssociation author { login __typename } } } }
      }
      comments(last: 50) { nodes { databaseId body createdAt authorAssociation author { login __typename } } }
      headCommit: commits(last: 1) {
        nodes { commit { statusCheckRollup { state contexts(first: 50) { nodes {
          __typename
          ... on CheckRun { name conclusion status detailsUrl title summary text }
          ... on StatusContext { context state description targetUrl }
        } } } } }
      }
    }
  }
}
"""


def _author(node: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    node = node or {}
    return {"login": node.get("login"), "bot": str(node.get("__typename") or "") == "Bot"}


def _comment(raw: Dict[str, Any]) -> Dict[str, Any]:
    author = _author(raw.get("author"))
    return {
        "id": raw.get("databaseId"),
        "body": str(raw.get("body") or ""),
        "path": raw.get("path"),
        "line": raw.get("line") or raw.get("originalLine"),
        "created_at": raw.get("createdAt"),
        "created_ms": _iso_to_ms(str(raw.get("createdAt") or "")),
        "author": author["login"],
        "bot": author["bot"],
        "author_association": str(raw.get("authorAssociation") or "NONE").upper(),
    }


def normalize_pull_state(node: Dict[str, Any]) -> Dict[str, Any]:
    state = str(node.get("state") or "OPEN").upper()
    commits = []
    for entry in ((node.get("commits") or {}).get("nodes") or []):
        commit = (entry or {}).get("commit") or {}
        author = commit.get("author") or {}
        committer = commit.get("committer") or {}
        commits.append({
            "sha": commit.get("oid"),
            "at": commit.get("committedDate"),
            "at_ms": _iso_to_ms(str(commit.get("committedDate") or "")),
            "author_name": author.get("name"),
            "author_email": author.get("email"),
            "author_login": (author.get("user") or {}).get("login"),
            "committer_email": committer.get("email"),
            "committer_login": (committer.get("user") or {}).get("login"),
        })
    reviews = []
    for raw in ((node.get("latestReviews") or {}).get("nodes") or []):
        author = _author((raw or {}).get("author"))
        reviews.append({"state": str((raw or {}).get("state") or "").upper(), "author": author["login"], "bot": author["bot"], "at": (raw or {}).get("submittedAt")})
    threads = []
    for raw in ((node.get("reviewThreads") or {}).get("nodes") or []):
        first = (((raw or {}).get("comments") or {}).get("nodes") or [None])[0]
        threads.append({
            "resolved": bool((raw or {}).get("isResolved")),
            "resolved_by": ((raw or {}).get("resolvedBy") or {}).get("login"),
            "comment": _comment(first) if first else None,
        })
    issue_comments = [_comment(raw) for raw in ((node.get("comments") or {}).get("nodes") or []) if raw]
    rollup = None
    head_nodes = ((node.get("headCommit") or {}).get("nodes") or [])
    if head_nodes:
        rollup = ((head_nodes[0] or {}).get("commit") or {}).get("statusCheckRollup")
    items = []
    for ctx in (((rollup or {}).get("contexts") or {}).get("nodes") or []):
        if not isinstance(ctx, dict):
            continue
        if ctx.get("__typename") == "CheckRun":
            conclusion = str(ctx.get("conclusion") or "").upper()
            status = str(ctx.get("status") or "").upper()
            items.append({
                "name": ctx.get("name"),
                "status": "pending" if status != "COMPLETED" else ("failure" if conclusion in {"FAILURE", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED", "STARTUP_FAILURE"} else "success"),
                "conclusion": conclusion.lower() or None,
                "summary": ctx.get("summary") or ctx.get("title"),
                "text": ctx.get("text"),
                "url": ctx.get("detailsUrl"),
            })
        else:
            st = str(ctx.get("state") or "").upper()
            items.append({
                "name": ctx.get("context"),
                "status": "pending" if st in {"PENDING", "EXPECTED"} else ("failure" if st in {"FAILURE", "ERROR"} else "success"),
                "conclusion": st.lower() or None,
                "summary": ctx.get("description"),
                "text": None,
                "url": ctx.get("targetUrl"),
            })
    checks = {
        "state": str((rollup or {}).get("state") or "").lower() or None,
        "total": len(items),
        "failed": sum(1 for i in items if i["status"] == "failure"),
        "pending": sum(1 for i in items if i["status"] == "pending"),
        "items": items,
    }
    return {
        "state": "merged" if node.get("merged") or state == "MERGED" else ("closed" if state == "CLOSED" else "open"),
        "merged_at": node.get("mergedAt"),
        "closed_at": node.get("closedAt"),
        "updated_at": node.get("updatedAt"),
        "updated_ms": _iso_to_ms(str(node.get("updatedAt") or "")),
        "title": node.get("title"),
        "draft": bool(node.get("isDraft")),
        "head_sha": node.get("headRefOid"),
        "branch": node.get("headRefName"),
        "base_branch": node.get("baseRefName"),
        "mergeable": str(node.get("mergeable") or "").lower() or None,
        "mergeable_state": str(node.get("mergeStateStatus") or "").lower() or None,
        "review_decision": str(node.get("reviewDecision") or "").upper() or None,
        "auto_merge": bool(node.get("autoMergeRequest")),
        "labels": [str((l or {}).get("name")) for l in ((node.get("labels") or {}).get("nodes") or []) if (l or {}).get("name")],
        "commits": commits,
        "reviews": reviews,
        "threads": threads,
        "issue_comments": issue_comments,
        "checks": checks,
    }
