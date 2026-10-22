"""The fakes the end-to-end suite runs Compass against (docs/e2e.md).

One small server with four parts, each under its own path:

    /github/…     the GitHub REST and GraphQL API, as much of it as Compass calls, over
                  repositories kept in memory (GITHUB_API_URL points here)
    /git/…        the same repositories over git's smart HTTP (GITHUB_GIT_URL points here):
                  `git http-backend` over a bare copy synced from memory before each request
                  and read back after a push
    /hooks/<name> a webhook receiver that records every call (notification endpoints)
    /feed/…       the release feed (COMPASS_UPDATE_URL points here) and the release notes
                  (COMPASS_RELEASE_NOTES_URL)
    /catalog.json the captain catalog (COMPASS_CATALOG_URL points here)

and a control API under /_control/… the tests use to arrange state (seed a repository,
merge a pull request, post a review comment, publish a release) and to inspect what
Compass did (the pull requests it opened, the webhooks it called, the feed requests).
Nothing here is Compass code; it only has to look like GitHub to PyGithub and requests.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import itertools
import os
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response

app = FastAPI(title="Compass e2e fakes")

LOGIN = "compass-e2e"
_ids = itertools.count(1000)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sha(*parts: Any) -> str:
    return hashlib.sha1("\0".join(str(p) for p in parts).encode()).hexdigest()


# ── state ───────────────────────────────────────────────────────────────────

class Repo:
    def __init__(self, full_name: str, files: Dict[str, str], default_branch: str = "main"):
        self.full_name = full_name
        self.owner, self.name = full_name.split("/", 1)
        self.default_branch = default_branch
        self.branches: Dict[str, Dict[str, bytes]] = {default_branch: {p: c.encode() for p, c in files.items()}}
        self.heads: Dict[str, str] = {default_branch: _sha(full_name, default_branch, time.time())}
        self.pulls: List[Dict[str, Any]] = []
        self.commits: Dict[int, List[Dict[str, Any]]] = {}

    def touch(self, branch: str) -> str:
        self.heads[branch] = _sha(self.full_name, branch, time.time(), next(_ids))
        return self.heads[branch]


STATE: Dict[str, Any] = {}


def reset() -> None:
    STATE.clear()
    STATE.update(repos={}, hooks=[], feed=None, feed_requests=[], notes=None, notes_requests=[], catalog={"schema": 1, "captains": []},
                 comments={}, graphql_requests=0)


reset()


def _repo(owner: str, name: str) -> Repo:
    repo = STATE["repos"].get(f"{owner}/{name}")
    if repo is None:
        raise HTTPException(404, {"message": "Not Found"})
    return repo


def _base(request: Request) -> str:
    return str(request.base_url).rstrip("/") + "/github"


def _user(login: str = LOGIN, kind: str = "User") -> Dict[str, Any]:
    return {"login": login, "id": abs(hash(login)) % 10**6, "type": kind, "url": f"/users/{login}", "html_url": f"https://github.example.com/{login}"}


def _repo_json(request: Request, repo: Repo) -> Dict[str, Any]:
    url = f"{_base(request)}/repos/{repo.full_name}"
    return {
        "id": abs(hash(repo.full_name)) % 10**6, "name": repo.name, "full_name": repo.full_name, "private": True, "archived": False,
        "owner": {**_user(repo.owner, "Organization"), "url": f"{_base(request)}/users/{repo.owner}"},
        "default_branch": repo.default_branch, "url": url, "html_url": f"https://github.example.com/{repo.full_name}",
        "pulls_url": url + "/pulls{/number}", "contents_url": url + "/contents/{+path}",
    }


def _content_json(request: Request, repo: Repo, path: str, data: bytes, ref: str) -> Dict[str, Any]:
    url = f"{_base(request)}/repos/{repo.full_name}/contents/{path}?ref={ref}"
    return {
        "type": "file", "encoding": "base64", "content": base64.b64encode(data).decode(), "size": len(data),
        "name": path.rsplit("/", 1)[-1], "path": path, "sha": _sha(path, data), "url": url,
        "html_url": f"https://github.example.com/{repo.full_name}/blob/{ref}/{path}",
    }


def _pull_json(request: Request, repo: Repo, pull: Dict[str, Any]) -> Dict[str, Any]:
    url = f"{_base(request)}/repos/{repo.full_name}/pulls/{pull['number']}"
    issue = f"{_base(request)}/repos/{repo.full_name}/issues/{pull['number']}"
    repo_json = _repo_json(request, repo)
    return {
        "id": pull["id"], "number": pull["number"], "state": pull["state"], "title": pull["title"], "body": pull["body"],
        "merged": pull["merged"], "merged_at": pull.get("merged_at"), "closed_at": pull.get("closed_at"), "draft": False,
        "url": url, "html_url": f"https://github.example.com/{repo.full_name}/pull/{pull['number']}",
        "issue_url": issue, "comments_url": issue + "/comments", "review_comments_url": url + "/comments",
        "head": {"ref": pull["head"], "sha": repo.heads.get(pull["head"], ""), "label": f"{repo.owner}:{pull['head']}", "repo": repo_json},
        "base": {"ref": pull["base"], "sha": repo.heads.get(pull["base"], ""), "label": f"{repo.owner}:{pull['base']}", "repo": repo_json},
        "user": _user(), "labels": [{"name": n} for n in pull["labels"]], "created_at": pull["created_at"], "updated_at": pull["updated_at"],
        "mergeable": True, "mergeable_state": "clean",
    }


def _find_pull(repo: Repo, number: int) -> Dict[str, Any]:
    for pull in repo.pulls:
        if pull["number"] == number:
            return pull
    raise HTTPException(404, {"message": "Not Found"})


# ── GitHub: users and repositories ──────────────────────────────────────────

@app.middleware("http")
async def refuse_bad_tokens(request: Request, call_next):
    """A token that starts with "bad" is one GitHub rejects (the settings tests use it)."""
    auth = request.headers.get("authorization") or ""
    if request.url.path.startswith("/github/") and auth.split(" ", 1)[-1].startswith("bad"):
        return JSONResponse(status_code=401, content={"message": "Bad credentials"})
    return await call_next(request)


@app.get("/github/user")
def get_user() -> Dict[str, Any]:
    return _user()


@app.get("/github/users/{login}")
def get_named_user(login: str) -> Dict[str, Any]:
    return _user(login)


@app.get("/github/user/repos")
@app.get("/github/orgs/{org}/repos")
@app.get("/github/users/{login}/repos")
def list_repos(request: Request, org: Optional[str] = None, login: Optional[str] = None, page: int = 1) -> List[Dict[str, Any]]:
    if page > 1:
        return []
    owner = org or login
    return [_repo_json(request, r) for r in STATE["repos"].values() if owner is None or r.owner == owner]


@app.get("/github/repos/{owner}/{name}")
def get_repo(owner: str, name: str, request: Request) -> Dict[str, Any]:
    return _repo_json(request, _repo(owner, name))


@app.get("/github/repos/{owner}/{name}/branches/{branch:path}")
def get_branch(owner: str, name: str, branch: str, request: Request) -> Dict[str, Any]:
    repo = _repo(owner, name)
    if branch not in repo.branches:
        raise HTTPException(404, {"message": "Branch not found"})
    sha = repo.heads[branch]
    return {"name": branch, "commit": {"sha": sha, "url": f"{_base(request)}/repos/{repo.full_name}/commits/{sha}"}, "protected": False}


@app.get("/github/repos/{owner}/{name}/git/ref/{ref:path}")
@app.get("/github/repos/{owner}/{name}/git/refs/{ref:path}")
def get_ref(owner: str, name: str, ref: str, request: Request) -> Dict[str, Any]:
    repo = _repo(owner, name)
    branch = ref.removeprefix("heads/")
    if branch not in repo.branches:
        raise HTTPException(404, {"message": "Not Found"})
    return {"ref": f"refs/heads/{branch}", "url": f"{_base(request)}/repos/{repo.full_name}/git/refs/heads/{branch}",
            "object": {"sha": repo.heads[branch], "type": "commit", "url": ""}}


@app.post("/github/repos/{owner}/{name}/git/refs", status_code=201)
async def create_ref(owner: str, name: str, request: Request) -> Any:
    repo = _repo(owner, name)
    body = await request.json()
    branch = str(body["ref"]).removeprefix("refs/heads/")
    if branch in repo.branches:
        return JSONResponse(status_code=422, content={"message": "Reference already exists"})
    source = next((b for b, sha in repo.heads.items() if sha == body.get("sha")), repo.default_branch)
    repo.branches[branch] = dict(repo.branches[source])
    repo.heads[branch] = body.get("sha") or repo.touch(branch)
    return {"ref": f"refs/heads/{branch}", "url": f"{_base(request)}/repos/{repo.full_name}/git/refs/heads/{branch}",
            "object": {"sha": repo.heads[branch], "type": "commit", "url": ""}}


@app.delete("/github/repos/{owner}/{name}/git/refs/{ref:path}", status_code=204)
def delete_ref(owner: str, name: str, ref: str) -> Response:
    repo = _repo(owner, name)
    branch = ref.removeprefix("heads/")
    if branch not in repo.branches or branch == repo.default_branch:
        raise HTTPException(422, {"message": "Reference does not exist"})
    repo.branches.pop(branch, None)
    repo.heads.pop(branch, None)
    return Response(status_code=204)


@app.get("/github/repos/{owner}/{name}/git/trees/{ref:path}")
def get_tree(owner: str, name: str, ref: str, request: Request) -> Dict[str, Any]:
    repo = _repo(owner, name)
    branch = ref if ref in repo.branches else next((b for b, sha in repo.heads.items() if sha == ref), None)
    if branch is None:
        raise HTTPException(404, {"message": "Not Found"})
    files = repo.branches[branch]
    return {
        "sha": repo.heads[branch], "url": f"{_base(request)}/repos/{repo.full_name}/git/trees/{repo.heads[branch]}", "truncated": False,
        "tree": [{"path": p, "mode": "100644", "type": "blob", "sha": _sha(p, d), "size": len(d), "url": ""} for p, d in sorted(files.items())],
    }


# ── GitHub: contents ────────────────────────────────────────────────────────

@app.get("/github/repos/{owner}/{name}/contents/{path:path}")
@app.get("/github/repos/{owner}/{name}/contents")
def get_contents(owner: str, name: str, request: Request, path: str = "", ref: Optional[str] = None) -> Any:
    repo = _repo(owner, name)
    branch = ref or repo.default_branch
    if branch not in repo.branches:
        raise HTTPException(404, {"message": "No commit found for the ref"})
    files = repo.branches[branch]
    path = path.strip("/")
    if path in files:
        return _content_json(request, repo, path, files[path], branch)
    prefix = f"{path}/" if path else ""
    children = sorted({p[len(prefix):].split("/", 1)[0] for p in files if p.startswith(prefix)})
    if not children:
        raise HTTPException(404, {"message": "Not Found"})
    out = []
    for child in children:
        full = prefix + child
        if full in files:
            out.append(_content_json(request, repo, full, files[full], branch))
        else:
            out.append({"type": "dir", "name": child, "path": full, "sha": _sha(full), "size": 0,
                        "url": f"{_base(request)}/repos/{repo.full_name}/contents/{full}?ref={branch}"})
    return out


@app.put("/github/repos/{owner}/{name}/contents/{path:path}")
async def put_contents(owner: str, name: str, path: str, request: Request) -> Any:
    repo = _repo(owner, name)
    body = await request.json()
    branch = body.get("branch") or repo.default_branch
    files = repo.branches.setdefault(branch, dict(repo.branches[repo.default_branch]))
    exists = path in files
    if exists and body.get("sha") != _sha(path, files[path]):
        return JSONResponse(status_code=409, content={"message": f"{path} does not match {body.get('sha')}"})
    if not exists and body.get("sha"):
        return JSONResponse(status_code=422, content={"message": "sha given for a file that does not exist"})
    files[path] = base64.b64decode(body["content"])
    sha = repo.touch(branch)
    _record_commit(repo, branch, sha, body.get("message") or "")
    return JSONResponse(status_code=200 if exists else 201, content={
        "content": _content_json(request, repo, path, files[path], branch),
        "commit": {"sha": sha, "url": f"{_base(request)}/repos/{repo.full_name}/git/commits/{sha}", "message": body.get("message")},
    })


@app.delete("/github/repos/{owner}/{name}/contents/{path:path}")
async def delete_contents(owner: str, name: str, path: str, request: Request) -> Any:
    repo = _repo(owner, name)
    body = await request.json()
    branch = body.get("branch") or repo.default_branch
    files = repo.branches.get(branch) or {}
    if path not in files:
        raise HTTPException(404, {"message": "Not Found"})
    del files[path]
    sha = repo.touch(branch)
    _record_commit(repo, branch, sha, body.get("message") or "")
    return {"content": None, "commit": {"sha": sha, "url": "", "message": body.get("message")}}


def _record_commit(repo: Repo, branch: str, sha: str, message: str) -> None:
    for pull in repo.pulls:
        if pull["head"] == branch and pull["state"] == "open":
            repo.commits.setdefault(pull["number"], []).append({"sha": sha, "message": message, "at": _now()})
            pull["updated_at"] = _now()


# ── GitHub: pull requests ───────────────────────────────────────────────────

@app.get("/github/repos/{owner}/{name}/pulls")
def list_pulls(owner: str, name: str, request: Request, state: str = "open", head: Optional[str] = None, base: Optional[str] = None,
               page: int = 1) -> List[Dict[str, Any]]:
    repo = _repo(owner, name)
    if page > 1:
        return []
    out = []
    for pull in repo.pulls:
        if state != "all" and pull["state"] != state:
            continue
        if head and f"{repo.owner}:{pull['head']}" != head and pull["head"] != head:
            continue
        if base and pull["base"] != base:
            continue
        out.append(_pull_json(request, repo, pull))
    return out


@app.post("/github/repos/{owner}/{name}/pulls", status_code=201)
async def create_pull(owner: str, name: str, request: Request) -> Any:
    repo = _repo(owner, name)
    body = await request.json()
    if body["head"] not in repo.branches:
        return JSONResponse(status_code=422, content={"message": "Validation Failed", "errors": [{"field": "head", "code": "invalid"}]})
    if any(p["head"] == body["head"] and p["state"] == "open" for p in repo.pulls):
        return JSONResponse(status_code=422, content={"message": f"A pull request already exists for {repo.owner}:{body['head']}."})
    number = len(repo.pulls) + 1
    pull = {"id": next(_ids), "number": number, "title": body["title"], "body": body.get("body") or "", "head": body["head"],
            "base": body.get("base") or repo.default_branch, "state": "open", "merged": False, "labels": [], "reviewers": [],
            "created_at": _now(), "updated_at": _now()}
    repo.pulls.append(pull)
    repo.commits.setdefault(number, []).append({"sha": repo.heads[body["head"]], "message": body["title"], "at": _now()})
    return _pull_json(request, repo, pull)


@app.get("/github/repos/{owner}/{name}/pulls/{number}")
def get_pull(owner: str, name: str, number: int, request: Request) -> Dict[str, Any]:
    repo = _repo(owner, name)
    return _pull_json(request, repo, _find_pull(repo, number))


@app.patch("/github/repos/{owner}/{name}/pulls/{number}")
async def edit_pull(owner: str, name: str, number: int, request: Request) -> Dict[str, Any]:
    repo = _repo(owner, name)
    pull = _find_pull(repo, number)
    body = await request.json()
    for key in ("title", "body", "state"):
        if key in body:
            pull[key] = body[key]
    if body.get("state") == "closed" and not pull.get("closed_at"):
        pull["closed_at"] = _now()
    pull["updated_at"] = _now()
    return _pull_json(request, repo, pull)


@app.get("/github/repos/{owner}/{name}/pulls/{number}/files")
def pull_files(owner: str, name: str, number: int) -> List[Dict[str, Any]]:
    repo = _repo(owner, name)
    pull = _find_pull(repo, number)
    head, base = repo.branches.get(pull["head"], {}), repo.branches.get(pull["base"], {})
    changed = sorted(p for p in set(head) | set(base) if head.get(p) != base.get(p))
    return [{"filename": p, "status": "added" if p not in base else ("removed" if p not in head else "modified")} for p in changed]


@app.get("/github/repos/{owner}/{name}/pulls/{number}/commits")
def pull_commits(owner: str, name: str, number: int, page: int = 1) -> List[Dict[str, Any]]:
    repo = _repo(owner, name)
    _find_pull(repo, number)
    if page > 1:
        return []
    return [{"sha": c["sha"], "commit": {"message": c["message"], "author": {"name": LOGIN, "email": f"{LOGIN}@example.com", "date": c["at"]},
             "committer": {"name": LOGIN, "email": f"{LOGIN}@example.com", "date": c["at"]}}, "author": _user(), "committer": _user()}
            for c in repo.commits.get(number, [])]


def _comments(full_name: str, number: int, kind: str) -> List[Dict[str, Any]]:
    return STATE["comments"].setdefault((full_name, number, kind), [])


@app.get("/github/repos/{owner}/{name}/issues/{number}/comments")
@app.get("/github/repos/{owner}/{name}/pulls/{number}/comments")
@app.get("/github/repos/{owner}/{name}/pulls/{number}/reviews")
def list_comments(owner: str, name: str, number: int, request: Request, page: int = 1) -> List[Dict[str, Any]]:
    if page > 1:
        return []
    kind = request.url.path.rsplit("/", 1)[-1]
    if "/issues/" in request.url.path:
        kind = "issue"
    return list(_comments(f"{owner}/{name}", number, kind))


@app.post("/github/repos/{owner}/{name}/issues/{number}/comments", status_code=201)
async def create_issue_comment(owner: str, name: str, number: int, request: Request) -> Dict[str, Any]:
    body = await request.json()
    comment = {"id": next(_ids), "body": body["body"], "user": _user(), "author_association": "MEMBER", "created_at": _now(), "updated_at": _now()}
    _comments(f"{owner}/{name}", number, "issue").append(comment)
    return comment


@app.post("/github/repos/{owner}/{name}/pulls/{number}/reviews")
async def create_review(owner: str, name: str, number: int, request: Request) -> Dict[str, Any]:
    """A review by the signed-in login: {"body", "event": COMMENT | APPROVE | REQUEST_CHANGES}."""
    full = f"{owner}/{name}"
    pull = _find_pull(_repo(owner, name), number)
    body = await request.json()
    state = {"APPROVE": "APPROVED", "REQUEST_CHANGES": "CHANGES_REQUESTED"}.get(str(body.get("event") or "COMMENT"), "COMMENTED")
    review = {"id": next(_ids), "state": state, "user": _user(), "body": body.get("body") or "", "submitted_at": _now(), "author_association": "MEMBER"}
    _comments(full, number, "reviews").append(review)
    if body.get("body"):
        _comments(full, number, "issue").append({"id": next(_ids), "body": body["body"], "user": _user(), "author_association": "MEMBER",
                                                 "created_at": _now(), "updated_at": _now()})
    if state != "COMMENTED":
        pull["review_decision"] = state
    pull["updated_at"] = _now()
    return review


@app.post("/github/repos/{owner}/{name}/pulls/{number}/comments/{comment_id}/replies", status_code=201)
async def reply_review_comment(owner: str, name: str, number: int, comment_id: int, request: Request) -> Dict[str, Any]:
    body = await request.json()
    comment = {"id": next(_ids), "body": body["body"], "user": _user(), "in_reply_to_id": comment_id, "author_association": "MEMBER",
               "created_at": _now(), "path": None, "line": None}
    _comments(f"{owner}/{name}", number, "comments").append(comment)
    return comment


@app.post("/github/repos/{owner}/{name}/issues/{number}/labels")
async def add_labels(owner: str, name: str, number: int, request: Request) -> List[Dict[str, Any]]:
    pull = _find_pull(_repo(owner, name), number)
    payload = await request.json()
    # GitHub takes {"labels": [...]} and a bare list (PyGithub sends the list)
    for label in (payload.get("labels") if isinstance(payload, dict) else payload) or []:
        if label not in pull["labels"]:
            pull["labels"].append(label)
    return [{"name": n} for n in pull["labels"]]


@app.delete("/github/repos/{owner}/{name}/issues/{number}/labels/{label}")
def remove_label(owner: str, name: str, number: int, label: str) -> List[Dict[str, Any]]:
    pull = _find_pull(_repo(owner, name), number)
    if label not in pull["labels"]:
        raise HTTPException(404, {"message": "Label does not exist"})
    pull["labels"].remove(label)
    return [{"name": n} for n in pull["labels"]]


@app.post("/github/repos/{owner}/{name}/pulls/{number}/requested_reviewers", status_code=201)
async def request_reviewers(owner: str, name: str, number: int, request: Request) -> Dict[str, Any]:
    repo = _repo(owner, name)
    pull = _find_pull(repo, number)
    pull["reviewers"] = sorted(set(pull["reviewers"]) | set((await request.json()).get("reviewers") or []))
    return _pull_json(request, repo, pull)


@app.post("/github/graphql")
async def graphql(request: Request) -> Dict[str, Any]:
    """The one query Compass sends (captains/prs/github_pr_client.py PULL_STATE_QUERY)."""
    STATE["graphql_requests"] += 1
    variables = (await request.json()).get("variables") or {}
    repo = STATE["repos"].get(f"{variables.get('owner')}/{variables.get('name')}")
    pull = next((p for p in (repo.pulls if repo else []) if p["number"] == int(variables.get("number") or 0)), None)
    if pull is None:
        return {"data": {"repository": {"pullRequest": None}}}
    full = repo.full_name

    def author(c: Dict[str, Any]) -> Dict[str, Any]:
        return {"login": c["user"]["login"], "__typename": "Bot" if c["user"].get("type") == "Bot" else "User"}

    review_comments = [c for c in _comments(full, pull["number"], "comments") if not c.get("in_reply_to_id")]
    state = "MERGED" if pull["merged"] else pull["state"].upper()
    return {"data": {"repository": {"pullRequest": {
        "state": state, "merged": pull["merged"], "mergedAt": pull.get("merged_at"), "closedAt": pull.get("closed_at"),
        "updatedAt": pull["updated_at"], "title": pull["title"], "isDraft": False, "headRefOid": repo.heads.get(pull["head"]),
        "headRefName": pull["head"], "baseRefName": pull["base"], "mergeable": "MERGEABLE", "mergeStateStatus": "CLEAN",
        "reviewDecision": pull.get("review_decision"), "autoMergeRequest": None,
        "labels": {"nodes": [{"name": n} for n in pull["labels"]]},
        "commits": {"nodes": [{"commit": {"oid": c["sha"], "committedDate": c["at"],
                                          "author": {"name": LOGIN, "email": f"{LOGIN}@example.com", "user": {"login": LOGIN}},
                                          "committer": {"name": LOGIN, "email": f"{LOGIN}@example.com", "user": {"login": LOGIN}}}}
                              for c in repo.commits.get(pull["number"], [])[-10:]]},
        "latestReviews": {"nodes": [{"state": r["state"], "submittedAt": r["submitted_at"], "author": author(r)}
                                    for r in _comments(full, pull["number"], "reviews")]},
        "reviewThreads": {"nodes": [{"isResolved": False, "resolvedBy": None, "comments": {"nodes": [{
            "databaseId": c["id"], "body": c["body"], "path": c.get("path"), "line": c.get("line"), "originalLine": c.get("line"),
            "createdAt": c["created_at"], "authorAssociation": c["author_association"], "author": author(c)}]}} for c in review_comments]},
        "comments": {"nodes": [{"databaseId": c["id"], "body": c["body"], "createdAt": c["created_at"],
                                "authorAssociation": c["author_association"], "author": author(c)}
                               for c in _comments(full, pull["number"], "issue")]},
        "headCommit": {"nodes": [{"commit": {"statusCheckRollup": {"state": "SUCCESS", "contexts": {"nodes": []}}}}]},
    }}}}


# ── git over smart HTTP ─────────────────────────────────────────────────────

GIT_ROOT = os.getenv("FAKES_GIT_ROOT") or tempfile.mkdtemp(prefix="fakes-git-")
_GIT_ENV = {**os.environ, "GIT_AUTHOR_NAME": LOGIN, "GIT_AUTHOR_EMAIL": f"{LOGIN}@example.com",
            "GIT_COMMITTER_NAME": LOGIN, "GIT_COMMITTER_EMAIL": f"{LOGIN}@example.com"}


def _git(bare: str, *args: str, stdin: Optional[bytes] = None, env: Optional[Dict[str, str]] = None) -> bytes:
    return subprocess.run(["git", "-C", bare, *args], input=stdin, capture_output=True, check=True, env={**_GIT_ENV, **(env or {})}).stdout


def _bare(repo: Repo) -> str:
    path = os.path.join(GIT_ROOT, f"{repo.full_name}.git")
    if not os.path.isdir(path):
        os.makedirs(path)
        subprocess.run(["git", "init", "--bare", "-q", "-b", repo.default_branch, path], check=True)
        _git(path, "config", "http.receivepack", "true")
    return path


def _sync_to_disk(repo: Repo) -> None:
    """Commit each branch's files from memory onto the bare repository, where they differ."""
    bare = _bare(repo)
    for branch, files in repo.branches.items():
        index = os.path.join(bare, f"index-{branch.replace('/', '_')}")
        if os.path.exists(index):
            os.remove(index)
        for path, data in files.items():
            blob = _git(bare, "hash-object", "-w", "--stdin", stdin=data).decode().strip()
            _git(bare, "update-index", "--add", "--cacheinfo", f"100644,{blob},{path}", env={"GIT_INDEX_FILE": index})
        tree = _git(bare, "write-tree", env={"GIT_INDEX_FILE": index}).decode().strip() if files else \
            _git(bare, "hash-object", "-t", "tree", "-w", "--stdin", stdin=b"").decode().strip()
        try:
            head = _git(bare, "rev-parse", "--verify", "-q", f"refs/heads/{branch}").decode().strip()
            current = _git(bare, "rev-parse", f"{head}^{{tree}}").decode().strip()
        except subprocess.CalledProcessError:
            head, current = "", ""
        if current == tree:
            continue
        parent = ["-p", head] if head else []
        commit = _git(bare, "commit-tree", tree, *parent, "-m", f"sync {branch}").decode().strip()
        _git(bare, "update-ref", f"refs/heads/{branch}", commit)


def _sync_from_disk(repo: Repo) -> None:
    """After a push: every branch on disk whose files differ from memory replaces them."""
    bare = _bare(repo)
    refs = _git(bare, "for-each-ref", "--format=%(refname:short) %(objectname)", "refs/heads").decode().split("\n")
    for line in filter(None, refs):
        branch, commit = line.split(" ", 1)
        files: Dict[str, bytes] = {}
        listing = _git(bare, "ls-tree", "-r", "-z", commit).decode().split("\0")
        for entry in filter(None, listing):
            meta, path = entry.split("\t", 1)
            files[path] = _git(bare, "cat-file", "blob", meta.split()[2])
        if repo.branches.get(branch) != files:
            repo.branches[branch] = files
            repo.heads[branch] = commit
            message = _git(bare, "log", "-1", "--format=%s", commit).decode().strip()
            _record_commit(repo, branch, commit, message)


@app.api_route("/git/{owner}/{name}.git/{rest:path}", methods=["GET", "POST"])
async def git_http(owner: str, name: str, rest: str, request: Request) -> Response:
    """git clone, fetch and push, through git's own CGI."""
    repo = _repo(owner, name)
    await asyncio.to_thread(_sync_to_disk, repo)
    body = await request.body()
    env = {
        **os.environ, "GIT_PROJECT_ROOT": os.path.join(GIT_ROOT, owner), "GIT_HTTP_EXPORT_ALL": "1",
        "PATH_INFO": f"/{name}.git/{rest}", "REQUEST_METHOD": request.method, "QUERY_STRING": request.url.query,
        "CONTENT_TYPE": request.headers.get("content-type", ""), "CONTENT_LENGTH": str(len(body)), "REMOTE_USER": LOGIN,
        "REMOTE_ADDR": "127.0.0.1", "HTTP_CONTENT_ENCODING": request.headers.get("content-encoding", ""),
        "GIT_PROTOCOL": request.headers.get("git-protocol", ""),
    }
    result = await asyncio.to_thread(subprocess.run, ["git", "http-backend"], input=body, capture_output=True, env=env)
    head, _, payload = result.stdout.partition(b"\r\n\r\n")
    if not _:
        head, _, payload = result.stdout.partition(b"\n\n")
    status, headers = 200, {}
    for line in head.decode(errors="replace").splitlines():
        key, _, value = line.partition(":")
        if key.lower() == "status":
            status = int(value.strip().split()[0])
        elif key:
            headers[key.strip()] = value.strip()
    if rest == "git-receive-pack":
        await asyncio.to_thread(_sync_from_disk, repo)
    return Response(content=payload, status_code=status, headers=headers)


# ── webhooks, the release feed, the catalog ────────────────────────────────

@app.post("/hooks/{name}")
async def hook(name: str, request: Request) -> Dict[str, Any]:
    raw = await request.body()
    try:
        body: Any = await request.json()
    except Exception:  # noqa: BLE001 - a form or text body is recorded as text
        body = raw.decode(errors="replace")
    STATE["hooks"].append({"name": name, "body": body, "headers": dict(request.headers), "at": time.time()})
    return {"ok": True}


@app.get("/feed/releases.json")
def feed(request: Request) -> Any:
    STATE["feed_requests"].append({"at": time.time(), "user_agent": request.headers.get("user-agent")})
    if STATE["feed"] is None:
        raise HTTPException(404, "no release published")
    return STATE["feed"]


@app.get("/feed/notes.xml")
def notes(request: Request) -> Any:
    STATE["notes_requests"].append({"at": time.time(), "user_agent": request.headers.get("user-agent")})
    if STATE["notes"] is None:
        raise HTTPException(404, "no release notes published")
    return Response(content=STATE["notes"], media_type="application/rss+xml")


@app.get("/catalog.json")
def catalog() -> Dict[str, Any]:
    return STATE["catalog"]


# ── the control API (tests only) ────────────────────────────────────────────

@app.post("/_control/reset")
def control_reset() -> Dict[str, Any]:
    reset()
    return {"ok": True}


@app.put("/_control/repos/{owner}/{name}")
async def control_seed_repo(owner: str, name: str, request: Request) -> Dict[str, Any]:
    """Create (or replace) a repository with the given files on its default branch."""
    body = await request.json()
    STATE["repos"][f"{owner}/{name}"] = Repo(f"{owner}/{name}", body.get("files") or {}, body.get("default_branch") or "main")
    return {"ok": True}


@app.get("/_control/repos/{owner}/{name}")
def control_repo(owner: str, name: str) -> Dict[str, Any]:
    repo = _repo(owner, name)
    return {
        "branches": {b: sorted(f) for b, f in repo.branches.items()},
        "pulls": [{**p, "files": pull_files(owner, name, p["number"]),
                   "commits": repo.commits.get(p["number"], []),
                   "comments": _comments(repo.full_name, p["number"], "issue") + _comments(repo.full_name, p["number"], "comments")}
                  for p in repo.pulls],
    }


@app.get("/_control/repos/{owner}/{name}/file")
def control_file(owner: str, name: str, path: str, branch: str) -> Dict[str, Any]:
    data = (_repo(owner, name).branches.get(branch) or {}).get(path)
    if data is None:
        raise HTTPException(404, "no such file")
    return {"path": path, "text": data.decode(errors="replace")}


@app.post("/_control/repos/{owner}/{name}/pulls/{number}/merge")
def control_merge(owner: str, name: str, number: int) -> Dict[str, Any]:
    """Merge: the head's files become the base's, the pull request is closed and merged."""
    repo = _repo(owner, name)
    pull = _find_pull(repo, number)
    repo.branches[pull["base"]] = dict(repo.branches[pull["head"]])
    repo.touch(pull["base"])
    pull.update(state="closed", merged=True, merged_at=_now(), closed_at=_now(), updated_at=_now())
    return {"ok": True}


@app.post("/_control/repos/{owner}/{name}/pulls/{number}/close")
def control_close(owner: str, name: str, number: int) -> Dict[str, Any]:
    pull = _find_pull(_repo(owner, name), number)
    pull.update(state="closed", closed_at=_now(), updated_at=_now())
    return {"ok": True}


@app.post("/_control/repos/{owner}/{name}/pulls/{number}/review")
async def control_review(owner: str, name: str, number: int, request: Request) -> Dict[str, Any]:
    """A reviewer's comment: {"body", "path"?, "line"?, "login"?, "association"?, "state"?}. With a path it is a
    review comment on a line; without, an issue comment. "state" (CHANGES_REQUESTED, APPROVED) adds a review."""
    body = await request.json()
    full = f"{owner}/{name}"
    pull = _find_pull(_repo(owner, name), number)
    user = _user(body.get("login") or "reviewer")
    comment = {"id": next(_ids), "body": body["body"], "user": user, "author_association": body.get("association") or "MEMBER",
               "created_at": _now(), "updated_at": _now(), "path": body.get("path"), "line": body.get("line")}
    _comments(full, number, "comments" if body.get("path") else "issue").append(comment)
    if body.get("state"):
        _comments(full, number, "reviews").append({"id": next(_ids), "state": body["state"], "user": user, "body": body["body"],
                                                   "submitted_at": _now(), "author_association": comment["author_association"]})
        pull["review_decision"] = body["state"]
    pull["updated_at"] = _now()
    return {"ok": True, "id": comment["id"]}


@app.get("/_control/hooks")
def control_hooks() -> List[Dict[str, Any]]:
    return STATE["hooks"]


@app.put("/_control/feed")
async def control_feed(request: Request) -> Dict[str, Any]:
    STATE["feed"] = await request.json()
    return {"ok": True}


@app.get("/_control/feed/requests")
def control_feed_requests() -> List[Dict[str, Any]]:
    return STATE["feed_requests"]


@app.put("/_control/notes")
async def control_notes(request: Request) -> Dict[str, Any]:
    STATE["notes"] = (await request.body()).decode() or None  # an empty body unpublishes them
    return {"ok": True}


@app.get("/_control/notes/requests")
def control_notes_requests() -> List[Dict[str, Any]]:
    return STATE["notes_requests"]


@app.put("/_control/catalog")
async def control_catalog(request: Request) -> Dict[str, Any]:
    STATE["catalog"] = await request.json()
    return {"ok": True}


@app.get("/health")
def health() -> Dict[str, Any]:
    return {"ok": True}
