"""The fix pull request (docs/specs/patching-captain.md §3.5, §3.7): one per repository, every
fix that repository needs, on ``beamreach/patch/<yyyymmdd>-<short>``, regenerated from the
base when new findings land before it merges.

The edits are the strategies' (``fixes.py``), applied to the file as it is on the base
branch and committed through the Contents API; the body lists each finding (CVE, package,
from → to, the workload and whether it is exposed, KEV, EPSS, CVSS), what verification
applies and how to roll back, with one paragraph from the model when one is configured
and the template otherwise. The pull request is registered with the PR watcher, which
syncs it, revises within the touched files, and tells this module when it merged or
closed; the findings move to ``pr_open``, ``merged``, or back to ``open`` with a cooldown.
"""

from __future__ import annotations

import hashlib
import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional

from captains.patch import fixes as _fixes
from captains.patch.findings import FindingStore
from captains.patch.store import AdvisoryStore

logger = logging.getLogger("patch.pr")

BRANCH_PREFIX = "beamreach/patch/"
LABELS = ("compass:patch", "security")
EXPLOITED_LABEL = "compass:exploited"
PR_TYPE = "patch_fix"
CLOSED_COOLDOWN_DAYS = 14
DEFAULT_MAX_OPEN = 10


# ── grouping ──────────────────────────────────────────────────────────────────

def allowed_repo(repo: str, config: Mapping[str, Any]) -> bool:
    section = config.get("patch") if isinstance(config.get("patch"), dict) else {}
    watcher = ((config.get("prs") or {}).get("watcher") or {}) if isinstance(config.get("prs"), dict) else {}
    for allowed in (section.get("repos") or [], watcher.get("repos") or []):
        if allowed and repo not in allowed:
            return False
    return True


def pending(findings: Iterable[Mapping[str, Any]], config: Mapping[str, Any], *, now_ms: Optional[int] = None) -> Dict[str, List[Dict[str, Any]]]:
    """Findings a pull request should carry, by repository: ``open`` with verdict ``pr`` and ``approved``,
    with a strategy, in a repository the captain may write to, outside a closed pull request's cooldown."""
    now = now_ms or int(time.time() * 1000)
    out: Dict[str, List[Dict[str, Any]]] = {}
    for row in findings:
        repo = row.get("repo")
        if not repo or not allowed_repo(str(repo), config):
            continue
        state = str(row.get("state") or "")
        if not ((state == "open" and row.get("verdict") == "pr") or state == "approved" or state == "pr_open"):
            continue
        if _fixes.strategy_for(row) is None:
            continue
        cooldown = (row.get("pr") or {}).get("cooldown_until")
        if state != "pr_open" and cooldown and int(cooldown) > now:
            continue
        out.setdefault(str(repo), []).append(dict(row))
    return out


def branch_name(repo: str, findings: Iterable[Mapping[str, Any]], *, today: Optional[datetime] = None) -> str:
    """``beamreach/patch/<yyyymmdd>-<short>``: the day the pull request first opened and a hash of the
    repository, so one repository has one patch branch at a time; new findings land on it."""
    existing = next((str((f.get("pr") or {}).get("branch")) for f in findings if (f.get("pr") or {}).get("branch")), None)
    if existing:
        return existing
    day = (today or datetime.now(timezone.utc)).strftime("%Y%m%d")
    return f"{BRANCH_PREFIX}{day}-{hashlib.sha1(repo.encode('utf-8')).hexdigest()[:6]}"


# ── the body ──────────────────────────────────────────────────────────────────

def _advisory_links(advisory_id: str) -> str:
    if advisory_id.startswith("CVE-"):
        return f"[{advisory_id}](https://nvd.nist.gov/vuln/detail/{advisory_id})"
    if advisory_id.startswith("GHSA-"):
        return f"[{advisory_id}](https://github.com/advisories/{advisory_id})"
    return f"[{advisory_id}](https://osv.dev/vulnerability/{advisory_id})"


def body(repo: str, findings: List[Mapping[str, Any]], edits: Mapping[str, _fixes.Edit], *, explanation: Optional[str] = None,
         verification: Optional[Mapping[str, Any]] = None) -> str:
    advisories = sorted({str(f.get("advisory_id")) for f in findings})
    exposed = [f for f in findings if f.get("exposed")]
    kev = any(f.get("kev") for f in findings)
    lines = [f"Compass found {len(findings)} vulnerable version{'s' if len(findings) != 1 else ''} in this repository and moved each to the version that fixes it. "
             f"{len(exposed)} of the workloads running them face{'s' if len(exposed) == 1 else ''} the internet." + (" One of the vulnerabilities is exploited in the wild (CISA KEV)." if kev else ""), ""]
    if explanation:
        lines += [explanation.strip(), ""]
    lines += ["## Findings", "", "| Advisory | Package | From → to | Where it runs | Why now |", "|---|---|---|---|---|"]
    for f in sorted(findings, key=lambda r: (not r.get("exposed"), -(r.get("cvss") or 0), str(r.get("advisory_id")))):
        where = ", ".join(f"{w.get('name')}{' (internet-facing)' if w.get('exposed') else ''}" for w in f.get("workloads") or []) or f"{f.get('path')} (no workload on the map)"
        why = "; ".join(str(r) for r in (f.get("verdict_reasons") or []) if not str(r).startswith("waits"))
        score = " · ".join(filter(None, [f"CVSS {f['cvss']}" if f.get("cvss") is not None else None, "KEV" if f.get("kev") else None,
                                         f"EPSS {float(f['epss']):.0%}" if f.get("epss") is not None else None]))
        lines.append(f"| {_advisory_links(str(f.get('advisory_id')))} {score} | {f.get('package')} | {f.get('version') or 'unpinned'} → {f.get('fixed_version')} | {where} | {why} |")
    lines += ["", "## Changes", ""]
    for path, edit in sorted(edits.items()):
        lines.append(f"- `{path}`: {edit.description}")
        for note in edit.notes:
            lines.append(f"  - {note}")
    lines += ["", "## Verification", ""]
    checks = verification or {}
    lines.append(f"- CI: {checks.get('ci', 'the repository’s checks on this pull request')}")
    lines.append(f"- Image build: {checks.get('build', 'the repository’s build workflow, where the fix touches a Dockerfile')}")
    lines.append(f"- Canary: {checks.get('canary', 'not configured for these workloads; set patch.canaries to run one on a staging twin')}")
    lines += ["", "## Rolling back", "", "Revert this pull request. Nothing else changed.", "",
              f"_Sources: {', '.join(advisories)}. Opened by Compass's patch captain; the PR watcher keeps it alive until you merge._"]
    return "\n".join(lines)


def explanation(model: Any, findings: List[Mapping[str, Any]], advisories: Iterable[Any]) -> Optional[str]:
    """One paragraph from the model: what the CVE is and why these workloads are affected, from the
    advisory's own text and the findings' facts. None without a model or when it fails."""
    if model is None:
        return None
    facts = [f"{f.get('advisory_id')}: {f.get('package')} {f.get('version') or 'unpinned'} on {', '.join(w.get('name', '') for w in f.get('workloads') or []) or f.get('path')}"
             f"{' (internet-facing)' if f.get('exposed') else ''}, fixed in {f.get('fixed_version')}" for f in findings]
    texts = [f"{a.id}: {a.summary}. {a.details[:600]}" for a in advisories if getattr(a, "summary", None)]
    prompt = ("Write one paragraph, at most 90 words, plain English, no bullet points, for the engineer who will review this pull request. "
              "Say what the vulnerability is and why these workloads are affected, quoting only the facts below. Do not invent versions, dates or impact.\n\n"
              "Advisories:\n" + "\n".join(texts) + "\n\nFindings:\n" + "\n".join(facts))
    try:
        text = model.call([{"role": "user", "content": prompt}], max_tokens=300, temperature=0.2)
    except Exception as exc:  # noqa: BLE001 - the template body stands without the model
        logger.debug("explanation not written: %s", exc)
        return None
    return str(text or "").strip() or None


# ── opening ───────────────────────────────────────────────────────────────────

def intent_for_patch(repo: str, findings: List[Mapping[str, Any]], files: List[str]) -> Dict[str, Any]:
    from captains.prs.intent import _base

    advisories = sorted({str(f.get("advisory_id")) for f in findings})
    return _base(
        goal=f"Move {len(findings)} vulnerable version{'s' if len(findings) != 1 else ''} in {repo} to the versions that fix {', '.join(advisories[:3])}{' and more' if len(advisories) > 3 else ''}",
        action_family="patch", done_criteria=[f"{f.get('package')} is at {f.get('fixed_version')} or later" for f in findings],
        safe_change_class="version_bump", rollback_expectation=["Revert the merge; the previous versions are restored by the next deploy."],
        patch_summary="Security patch", files=files, resource_ids=[str(w.get("node_id")) for f in findings for w in f.get("workloads") or []],
        invariants=[{"kind": "files_differ_from_base", "files": files}], verification=None, risk=1,
    )


class PullRequestOpener:
    """Opens or refreshes the fix pull request of one repository and records it everywhere it must be."""

    def __init__(self, config: Mapping[str, Any], *, workspace_id: str, findings: FindingStore, advisories: Optional[AdvisoryStore] = None,
                 github_client: Any = None, token_for_repo: Any = None, registry: Any = None, model: Any = None, http: Any = None,
                 now: Optional[datetime] = None, topology: Any = None):
        self.config = config
        self.workspace_id = workspace_id
        self.findings = findings
        self.advisories = advisories
        self._github_client = github_client
        self._token_for_repo = token_for_repo
        self._registry = registry
        self.model = model
        self.http = http
        self.now = now or datetime.now(timezone.utc)
        self.topology = topology

    def _repo(self, repo: str) -> Any:
        if self._github_client is not None:
            return self._github_client(repo)
        from compass_core.github import client as github_client
        from compass_core.github import resolve_github_token_for_repo

        token = (self._token_for_repo or resolve_github_token_for_repo)(self.config, repo_slug=repo, capability="write")
        if not token:
            raise RuntimeError(f"no GitHub write access for {repo}")
        return github_client(token).get_repo(repo)

    def registry(self) -> Any:
        if self._registry is None:
            from compass_core.registry import PRRegistry

            self._registry = PRRegistry.from_config(dict(self.config), workspace_id=self.workspace_id)
        return self._registry

    def open_all(self, *, now_ms: Optional[int] = None) -> Dict[str, Any]:
        """Every repository with pending findings, within ``max_open_prs``."""
        rows = self.findings.list()
        groups = pending(rows, self.config, now_ms=now_ms)
        section = self.config.get("patch") if isinstance(self.config.get("patch"), dict) else {}
        max_open = int(section.get("max_open_prs", DEFAULT_MAX_OPEN) or 0)
        already_open = {str(r.get("repo")) for r in rows if r.get("state") == "pr_open" and r.get("repo")}
        report: Dict[str, Any] = {"opened": [], "updated": [], "skipped": {}, "errors": {}}
        budget = max(0, max_open - len(already_open)) if max_open else None
        for repo, group in sorted(groups.items(), key=lambda kv: (not any(f.get("exposed") for f in kv[1]), kv[0])):
            is_update = repo in already_open
            if not is_update and budget is not None:
                if budget <= 0:
                    report["skipped"][repo] = f"max_open_prs ({max_open}) reached"
                    continue
            try:
                result = self.open(repo, group)
            except Exception as exc:  # noqa: BLE001 - one repository never stops the others
                logger.warning("patch pull request for %s failed: %s", repo, exc)
                report["errors"][repo] = f"{type(exc).__name__}: {exc}"[:300]
                continue
            if result is None:
                report["skipped"][repo] = "nothing to change on the base branch"
                continue
            (report["updated"] if result.get("updated") else report["opened"]).append(result)
            if not is_update and budget is not None:
                budget -= 1
        return report

    def open(self, repo: str, group: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        from compass_core.github_prs import commit_files, ensure_branch, open_or_update_pr

        gh_repo = self._repo(repo)
        github_cfg = self.config.get("github") if isinstance(self.config.get("github"), dict) else {}
        base_branch = str(github_cfg.get("default_branch") or getattr(gh_repo, "default_branch", None) or "main")
        branch = branch_name(repo, group, today=self.now)
        # the edits come from the base branch's content, so the pull request is regenerated, never appended to
        edits: List[_fixes.Edit] = []
        applied: List[Dict[str, Any]] = []
        contents: Dict[str, str] = {}
        digest = (lambda image, tag: _fixes.docker_hub_digest(self.http, image, tag)) if self.http is not None else None
        for finding in sorted(group, key=lambda f: (str(f.get("path")), str(f.get("advisory_id")))):
            path = _fixes.target_path(finding)
            if path not in contents:
                try:
                    blob = gh_repo.get_contents(path, ref=base_branch)
                    contents[path] = blob.decoded_content.decode("utf-8", errors="replace")
                except Exception as exc:  # noqa: BLE001 - a file the mapper saw and GitHub no longer serves
                    logger.debug("%s not read from %s: %s", path, repo, exc)
                    contents[path] = ""
            edit = _fixes.apply(finding, contents[path], digest_for_tag=digest)
            if edit is None or not edit.changed:
                continue
            contents[path] = edit.after
            edits.append(edit)
            applied.append(finding)
        if not edits:
            return None
        by_file = _fixes.group_by_file(edits)
        # the image must build before the fix is called ready: a minimal workflow when the repository has none that does
        from captains.patch import verify as _verify

        dockerfiles = [p for p in by_file if _verify.is_dockerfile(p)]
        workflows = (self.topology.repo_workflows.get(repo, []) if self.topology is not None else [])
        added_workflow = None
        if dockerfiles and not _verify.repo_builds_on_pull_requests(workflows):
            workflow_path = f".github/workflows/{_verify.BUILD_WORKFLOW}.yml"
            try:
                existing = gh_repo.get_contents(workflow_path, ref=base_branch).decoded_content.decode("utf-8", errors="replace")
            except Exception:  # noqa: BLE001 - 404: the repository has none
                existing = ""
            content = _verify.build_workflow(dockerfiles)
            if existing != content:
                added_workflow = _fixes.Edit(path=workflow_path, before=existing, after=content, strategy="build_workflow",
                                             description="a workflow that builds the changed Dockerfile on pull requests (the repository had none)")
                by_file[workflow_path] = added_workflow
        # a moved manifest: the lockfile beside it is regenerated on the branch by compass-lockfile.yml (open decision 1)
        from captains.patch import lockfiles as _lockfiles

        lock_mode = _lockfiles.mode(self.config)
        manifest_edits: Dict[str, List[Dict[str, Any]]] = {}
        for finding in applied:
            if _fixes.strategy_for(finding) == "manifest" and not str(finding.get("path") or "").rsplit("/", 1)[-1].lower().startswith("requirements"):
                manifest_edits.setdefault(_fixes.target_path(finding), []).append(finding)
        lock_entries: List[_lockfiles.LockEntry] = []
        if manifest_edits and lock_mode == "ci":
            known = {str(f.get("path")) for f in applied if str(f.get("path") or "").rsplit("/", 1)[-1] in _lockfiles.TOOLS}

            def exists(path: str) -> bool:
                try:
                    gh_repo.get_contents(path, ref=base_branch)
                    return True
                except Exception:  # noqa: BLE001 - 404
                    return False

            lock_entries = _lockfiles.plan(manifest_edits, exists, known_lockfiles=known)
            if lock_entries:
                try:
                    existing_lock_workflow = gh_repo.get_contents(_lockfiles.WORKFLOW_PATH, ref=base_branch).decoded_content.decode("utf-8", errors="replace")
                except Exception:  # noqa: BLE001
                    existing_lock_workflow = ""
                content = _lockfiles.workflow(lock_entries, author_email=_lockfiles.author_email(self.config))
                if existing_lock_workflow != content:
                    by_file[_lockfiles.WORKFLOW_PATH] = _fixes.Edit(path=_lockfiles.WORKFLOW_PATH, before=existing_lock_workflow, after=content, strategy="lockfile_workflow",
                                                                   description=f"a workflow that regenerates {', '.join(e.lockfile for e in lock_entries)} on this branch")
        for manifest in manifest_edits:
            entry = next((e for e in lock_entries if e.manifest == manifest), None)
            if manifest in by_file:
                by_file[manifest].notes.append(_lockfiles.note(entry, lock_mode))
        files = sorted(by_file)
        allowed_files = sorted(set(files) | {e.lockfile for e in lock_entries})
        advisories = [a for a in (self.advisories.get(str(f.get("advisory_id"))) for f in applied) if a] if self.advisories is not None else []
        text = body(repo, applied, by_file, explanation=explanation(self.model, applied, advisories))
        ids = sorted({str(f.get("advisory_id")) for f in applied})
        title = f"Security: {', '.join(ids[:2])}{f' and {len(ids) - 2} more' if len(ids) > 2 else ''} — {', '.join(sorted({str(f.get('package')) for f in applied})[:3])}"
        ensure_branch(gh_repo, branch, base_branch)
        commit_files(gh_repo, branch, {path: edit.after for path, edit in by_file.items()},
                     add_message="compass: security patch, add {path}", update_message="compass: security patch, {path}")
        published = open_or_update_pr(gh_repo, branch=branch, base_branch=base_branch, title=title, body=text)
        labels = list(LABELS) + ([EXPLOITED_LABEL] if any(f.get("kev") for f in applied) else [])
        try:
            if published.number is not None:
                gh_repo.get_pull(published.number).add_to_labels(*labels) if hasattr(gh_repo, "get_pull") else None
        except Exception as exc:  # noqa: BLE001 - labels are decoration
            logger.debug("labels on %s#%s not set: %s", repo, published.number, exc)
        self.registry().record(url=published.url, pr_type=PR_TYPE, origin={"captain": "patch", "finding_id": f"{repo}#{published.number}", "repo": repo,
                                                                            "finding_keys": [str(f.get("key")) for f in applied]},
                               intent=intent_for_patch(repo, applied, allowed_files), branch=branch, base_branch=base_branch, title=title)
        # the registry keeps the first intent's goal; this pull request's scope grows with each finding that lands on it
        fresh_intent = intent_for_patch(repo, applied, allowed_files)
        self.registry().widen_scope(repo, published.number, files=allowed_files, done_criteria=fresh_intent["done_criteria"], resource_ids=fresh_intent["scope"]["resource_ids"])
        pr_record = {"url": published.url, "number": published.number, "branch": branch, "base_branch": base_branch, "repo": repo, "opened_at": int(time.time() * 1000),
                     "files": files, "allowed_files": allowed_files, "lockfiles": [e.lockfile for e in lock_entries], "labels": labels, "updated": published.updated}
        previous = {str(f.get("key")): dict((f.get("pr") or {}).get("verification") or {}) for f in applied}
        for finding in applied:
            verification = previous.get(str(finding["key"])) or {}
            self.findings.set_pr(str(finding["key"]), state="pr_open", pr={**pr_record, **({"verification": verification} if verification else {})})
        if not published.updated:
            from captains.patch import notify as _notify

            _notify.emit(dict(self.config), self.workspace_id, "patch.pr_opened", {"repo": repo, "number": published.number, "url": published.url, "advisories": ", ".join(ids),
                                                                                  "kev": any(f.get("kev") for f in applied), "exposed": any(f.get("exposed") for f in applied),
                                                                                  "findings": len(applied)})
        try:
            from compass_core import inbox

            inbox.invalidate()
        except Exception:  # noqa: BLE001
            pass
        return {"repo": repo, "url": published.url, "number": published.number, "branch": branch, "files": files, "findings": len(applied), "updated": published.updated,
                "labels": labels}


# ── what the watcher tells us ─────────────────────────────────────────────────

def on_pr_status(finding_id: str, status: str, *, config: Mapping[str, Any], workspace_id: str, findings: Optional[FindingStore] = None,
                 now_ms: Optional[int] = None) -> Dict[str, Any]:
    """The watcher's status hook: ``finding_id`` is ``repo#number``; merged moves the pull request's
    findings to ``merged``, closed sends them back to ``open`` for ``CLOSED_COOLDOWN_DAYS`` without a new pull request."""
    if findings is None:
        from captains.patch.scheduler import stores

        _, findings = stores(dict(config), workspace_id)
    repo, _, number = str(finding_id).rpartition("#")
    now = now_ms or int(time.time() * 1000)
    moved: List[str] = []
    for row in findings.list(states=["pr_open"]):
        pr = row.get("pr") or {}
        if str(pr.get("repo")) != repo or str(pr.get("number")) != number:
            continue
        if status == "Resolved":
            findings.update(str(row["key"]), state="merged", pr={**pr, "merged_at": now})
        else:
            findings.update(str(row["key"]), state="open", pr={**pr, "closed_at": now, "cooldown_until": now + CLOSED_COOLDOWN_DAYS * 86_400_000, "closed_by": status})
        moved.append(str(row["key"]))
    return {"finding_id": finding_id, "status": status, "moved": moved}


def register_hook() -> None:
    try:
        from captains.prs import origins

        origins.register_status_hook("patch", lambda finding_id, status, **kw: on_pr_status(finding_id, status, config=kw["config"], workspace_id=kw["workspace_id"]))
    except Exception as exc:  # noqa: BLE001 - the watcher is optional
        logger.debug("status hook not registered: %s", exc)


register_hook()
