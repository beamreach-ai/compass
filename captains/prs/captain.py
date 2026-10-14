"""PRCaptain: act on the pull requests Compass opened.

``address`` takes one PR and one batch of feedback, decides under the workspace
policy, runs the guarded revision and tells the reviewers what happened.
Sync and the ticker (PR watcher PW-01/02) land next to it.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

from captains.prs import notify, origins, policy, triage
from captains.prs import sync as sync_rules
from captains.prs.context import load_origin_context
from captains.prs.github_pr_client import COMPASS_MARKER, GitHubPRClient
from captains.prs.intent import describe_invariant
from captains.prs.revision import run_revision
from compass_core.events import current_job_id, emit_job_event
from compass_core.github import resolve_github_token_for_repo
from compass_core.registry import PRRegistry, parse_pr_url

logger = logging.getLogger("prs-captain")

_REASON_TEXT = {
    "scope_expanded": "the change would reach outside the files this pull request touches",
    "invariant_broken": "the change would undo part of what this pull request exists to do",
    "verification_failed": "the revised configuration does not validate",
    "no_change": "the request did not lead to any change",
    "blocked_by_runtime": "the request conflicts with the purpose of this pull request",
    "conflict_outside_scope": "the branch conflicts with the base branch in files outside this pull request's scope",
    "execution_failed": "the revision could not be completed",
    "publish_failed": "the revision could not be pushed",
    "no_github_token": "no GitHub write access for this repository",
    "checkout_failed": "the branch could not be checked out",
    "no_revision_runner": "this installation does not push revisions",
}
_NEEDS_HUMAN_REASONS = frozenset({"scope_expanded", "invariant_broken", "blocked_by_runtime", "conflict_outside_scope", "no_revision_runner"})


def _short(text: Optional[str], limit: int = 160) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


class PRCaptain:
    def __init__(self, config: Dict[str, Any], workspace_ctx: Any = None, *, target: Optional[str] = None):
        self.config = config or {}
        self.workspace_ctx = workspace_ctx
        self.workspace_id = str(getattr(workspace_ctx, "workspace_id", None) or self.config.get("workspace_id") or "")
        self.target = target or str(self.config.get("target") or self.config.get("name") or self.workspace_id)
        self.registry = PRRegistry.from_config(self.config, workspace_id=self.workspace_id)

    # ── helpers ────────────────────────────────────────────────────────────

    def _record_from_payload(self, payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        repo = payload.get("repo")
        number = payload.get("number")
        if repo and number:
            return self.registry.get(str(repo), int(number))
        parsed = parse_pr_url(payload.get("pr_url"))
        if parsed:
            return self.registry.get(*parsed)
        finding_id = payload.get("finding_id")
        if finding_id:
            for record in self.registry.list(state="open"):
                if str((record.get("origin") or {}).get("finding_id") or "") == str(finding_id):
                    return record
        return None

    def _client(self, repo: str) -> Optional[GitHubPRClient]:
        token = resolve_github_token_for_repo(self.config, repo_slug=repo, capability="write")
        return GitHubPRClient(token) if token else None

    def _bedrock(self):
        """The text model for this workspace, or None (deterministic triage, template replies)."""
        try:
            from compass_core.llm import model_for

            return model_for(self.config, target=self.target)
        except Exception as exc:
            logger.debug("[prs] model unavailable: %s", exc)
            return None

    @staticmethod
    def _feedback_from_payload(payload: Dict[str, Any], fetched: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        raw = payload.get("review_feedback") if isinstance(payload.get("review_feedback"), dict) else {}
        comments: List[Dict[str, Any]] = []
        for comment in list(raw.get("comments") or []) + list((fetched or {}).get("comments") or []):
            if not isinstance(comment, dict):
                continue
            entry = dict(comment)
            entry.setdefault("kind", "change_in_scope")
            comments.append(entry)
        message = payload.get("message") or raw.get("message")
        if isinstance(message, str) and message.strip():
            comments.append({
                "id": "manual",
                "source": "manual",
                "path": None,
                "line": None,
                "body": message.strip(),
                "author": payload.get("user_id") or "operator",
                "author_association": "OWNER",
                "kind": "change_in_scope",
            })
        return {
            "trigger": str(payload.get("trigger") or "manual"),
            "comments": comments,
            "ci_failures": list(raw.get("ci_failures") or []),
            "conflicts": list(raw.get("conflicts") or []),
            "requested_changes_by": list((fetched or {}).get("requested_changes_by") or raw.get("requested_changes_by") or []),
            "head_sha": (fetched or {}).get("head_sha") or raw.get("head_sha"),
        }

    # ── address ────────────────────────────────────────────────────────────

    def address(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        record = self._record_from_payload(payload)
        if not record:
            raise RuntimeError("pull request is not in the registry")
        repo, number = str(record["repo"]), int(record["number"])
        settings = policy.watcher_settings(self.config)
        trigger = str(payload.get("trigger") or "manual")
        approved = bool(payload.get("approved"))
        job_id = current_job_id()

        client = self._client(repo)
        fetched = None
        if payload.get("fetch_comments") and client is not None:
            emit_job_event("Reading review comments", event_type="progress", status="running")
            try:
                fetched = client.fetch_open_feedback(repo, number, since_ms=(record.get("cursor") or {}).get("comment_ms"))
            except Exception as exc:
                emit_job_event(f"Could not read comments: {exc}", event_type="warning", status="running")
        feedback = self._feedback_from_payload(payload, fetched)
        intent = record.get("intent") if isinstance(record.get("intent"), dict) else {}
        bedrock = self._bedrock()
        origin_context: Optional[Dict[str, Any]] = None
        if any(not c.get("classified") and c.get("source") != "manual" for c in feedback["comments"]):
            emit_job_event("Loading what Compass knew about this change", event_type="progress", status="running")
            origin_context = load_origin_context(record, config=self.config, workspace_id=self.workspace_id)
            feedback["comments"] = triage.classify(feedback["comments"], intent=intent, origin_context=origin_context, bedrock=bedrock)
        actionable = [c for c in feedback["comments"] if c.get("kind") in triage.ACTIONABLE]
        ignored = [c for c in feedback["comments"] if c.get("kind") == "ignore"]
        to_reply = [c for c in feedback["comments"] if c.get("kind") in triage.REPLY_KINDS]
        replied = 0
        if to_reply and client is not None and settings.get("reply", True):
            replied = self._reply(client, repo, number, to_reply, intent=intent, origin_context=origin_context, bedrock=bedrock)
        contradicts = [c for c in to_reply if c.get("kind") == "contradicts_intent"]
        if contradicts:
            self._flag_human(client, repo, number, "contradicts_intent", {"comments": [c.get("id") for c in contradicts], "url": record.get("url")})
        elif not actionable and feedback.get("requested_changes_by") and any(c.get("kind") == "out_of_scope" for c in to_reply):
            self._flag_human(client, repo, number, "out_of_scope", {"by": feedback.get("requested_changes_by"), "url": record.get("url")})
        if not actionable and not feedback["ci_failures"] and not feedback["conflicts"]:
            self.registry.append_decision(repo, number, {"job_id": job_id, "trigger": trigger, "decision": "nothing_to_do", "ignored": len(ignored), "replied": replied, "kinds": sorted({str(c.get("kind")) for c in feedback["comments"]})})
            return {"status": "nothing_to_do", "repo": repo, "number": number, "ignored": len(ignored), "replied": replied}

        feedback["requested_files"] = policy.requested_files_from_feedback(feedback)
        risk = policy.revision_risk(record.get("intent") or {}, feedback["requested_files"])
        verdict = policy.decide(settings, risk=risk["risk"], record=record, approved=approved, trigger=trigger)
        decision = verdict.get("decision")
        plan = {
            "comments": [{k: c.get(k) for k in ("id", "path", "line", "author", "kind", "body")} for c in actionable],
            "files": feedback["requested_files"],
            "risk": risk,
            "mode": settings["mode"],
            "risk_max": settings["risk_max"],
            "trigger": trigger,
        }
        self.registry.append_decision(repo, number, {"job_id": job_id, "trigger": trigger, "decision": decision, "reason": verdict.get("reason"), "risk": risk["risk"], "comments": len(actionable)})

        if decision == "wait_for_approval":
            self.registry.update(repo, number, pending_revision={**plan, "created_at": int(time.time() * 1000), "feedback": feedback})
            emit_job_event(f"Revision planned; waiting for approval ({verdict.get('reason')})", event_type="progress", status="completed")
            return {"status": "pending_approval", "repo": repo, "number": number, "plan": plan, "reason": verdict.get("reason")}
        if decision != "run":
            emit_job_event(f"Revision not run: {verdict.get('reason')}", event_type="progress", status="completed")
            return {"status": decision, "repo": repo, "number": number, "reason": verdict.get("reason"), "plan": plan}

        if origin_context is None:
            emit_job_event("Loading what Compass knew about this change", event_type="progress", status="running")
            origin_context = load_origin_context(record, config=self.config, workspace_id=self.workspace_id)
        emit_job_event(f"Revising {repo}#{number}: {len(actionable)} request(s), risk {risk['risk']}", event_type="progress", status="running")
        result = run_revision(
            record=record, review_feedback=feedback, origin_context=origin_context, config=self.config,
            target=self.target, workspace_id=self.workspace_id, bedrock=bedrock, logger=logger, trigger=trigger,
        )
        outcome = str(result.get("outcome") or "failed")
        entry = {
            "job_id": job_id,
            "trigger": trigger,
            "outcome": outcome,
            "reason": result.get("reason"),
            "commit_sha": result.get("commit_sha"),
            "addressed": [c.get("id") for c in actionable],
            "files": result.get("changed_files") or [],
            "guard": result.get("guard"),
            "blockers": result.get("blockers") or [],
            "risk": risk["risk"],
            "note": _short(result.get("error") or result.get("summary"), 400),
            "context": {"summary": origin_context.get("summary"), "changed_since_pr": origin_context.get("changed_since_pr"), "notes": origin_context.get("notes")},
        }
        self.registry.append_revision(repo, number, entry)
        self.registry.update(repo, number, pending_revision=None, cursor={**(record.get("cursor") or {}), "comment_ms": int(time.time() * 1000)})
        if outcome == "refused" and result.get("reason") in _NEEDS_HUMAN_REASONS:
            self._flag_human(client, repo, number, str(result.get("reason")), {"url": record.get("url")})
        elif outcome == "pushed":
            notify.emit(self.config, self.workspace_id, "pr.revised", {"repo": repo, "number": number, "url": record.get("url"), "commit": result.get("commit_sha"), "addressed": len(actionable)})

        if client is not None and settings.get("reply", True):
            try:
                self._tell_reviewers(client, repo, number, actionable, ignored, feedback, result, entry)
            except Exception as exc:
                emit_job_event(f"Could not post replies: {exc}", event_type="warning", status="running")

        status_word = {"pushed": "completed", "refused": "completed", "failed": "failed"}.get(outcome, "completed")
        emit_job_event(
            f"Revision {outcome}" + (f": {result.get('reason')}" if result.get("reason") else "") + (f" ({str(result.get('commit_sha'))[:7]})" if result.get("commit_sha") else ""),
            event_type="progress" if outcome != "failed" else "error",
            status=status_word,
            data={"pr_url": record.get("url"), "outcome": outcome, "commit_sha": result.get("commit_sha")},
        )
        return {"status": outcome, "repo": repo, "number": number, "pr_url": record.get("url"), **{k: result.get(k) for k in ("reason", "commit_sha", "guard", "changed_files", "blockers", "error")}}

    def _flag_human(self, client: Optional[GitHubPRClient], repo: str, number: int, reason: str, payload: Dict[str, Any]) -> None:
        current = self.registry.get(repo, number) or {}
        if str((current.get("watch") or {}).get("status")) == "needs_human":
            return
        self.registry.set_watch(repo, number, "needs_human", reason)
        notify.emit(self.config, self.workspace_id, "pr.needs_human", {"repo": repo, "number": number, "reason": reason, **payload})
        if client is not None:
            try:
                client.add_labels(repo, number, [sync_rules.NEEDS_HUMAN_LABEL])
            except Exception as exc:
                logger.debug("[prs] label add failed: %s", exc)

    def _reply(self, client: GitHubPRClient, repo: str, number: int, comments, *, intent, origin_context, bedrock) -> int:
        replied = 0
        for comment in comments:
            text = triage.draft_reply(str(comment.get("kind")), comment, intent=intent, origin_context=origin_context, bedrock=bedrock)
            if not text:
                continue
            try:
                if comment.get("source") == "review_comment" and isinstance(comment.get("id"), int):
                    client.reply_to_review_comment(repo, number, comment["id"], f"{text}\n\n{COMPASS_MARKER}")
                else:
                    quote = " ".join(str(comment.get("body") or "").split())[:160]
                    client.create_issue_comment(repo, number, f"> {quote}\n\n{text}\n\n{COMPASS_MARKER}")
                replied += 1
            except Exception as exc:
                emit_job_event(f"Could not reply to comment {comment.get('id')}: {exc}", event_type="warning", status="running")
        return replied

    # ── sync ───────────────────────────────────────────────────────────────

    def sync(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Refresh every open PR (or one) from GitHub and act on what changed (spec §4.1)."""
        settings = policy.watcher_settings(self.config)
        identity = sync_rules.compass_identity(self.config)
        now = int(time.time() * 1000)
        force = bool(payload.get("force"))
        if payload.get("repo") and payload.get("number"):
            record = self.registry.get(str(payload["repo"]), int(payload["number"]))
            records = [record] if record else []
            force = True
        else:
            records = self.registry.list(state="open", limit=500)
        summary = {"status": "completed", "checked": 0, "skipped": 0, "triggers": 0, "events": 0, "errors": 0, "merged": 0, "closed": 0}
        from compass_core.queue import job_queue

        queue = job_queue()
        for record in records:
            repo, number = str(record.get("repo")), int(record.get("number") or 0)
            watch = record.get("watch") if isinstance(record.get("watch"), dict) else {}
            next_poll = watch.get("next_poll_at")
            if not force and isinstance(next_poll, (int, float)) and int(next_poll) > now:
                summary["skipped"] += 1
                continue
            client = self._client(repo)
            if client is None:
                summary["errors"] += 1
                emit_job_event(f"{repo}#{number}: no GitHub token", event_type="warning", status="running")
                continue
            try:
                snap = client.pull_request_state(repo, number)
            except Exception as exc:
                summary["errors"] += 1
                emit_job_event(f"{repo}#{number}: GitHub read failed: {exc}", event_type="warning", status="running")
                self.registry.update(repo, number, watch={**watch, "next_poll_at": now + sync_rules.POLL_SLOW_MS})
                continue
            plan = sync_rules.plan_sync(record, snap, settings=settings, now_ms=now, identity=identity)
            self.registry.update(repo, number, **plan.updates)
            summary["checked"] += 1
            if plan.updates.get("state") == "merged":
                summary["merged"] += 1
            if plan.updates.get("state") == "closed":
                summary["closed"] += 1
            for item in plan.bookkeeping:
                self._bookkeep(item)
            for event in plan.events:
                notify.emit(self.config, self.workspace_id, event["event"], {**event["payload"], "url": record.get("url")})
                summary["events"] += 1
            new_watch = plan.updates.get("watch") or {}
            if str(new_watch.get("status")) == "needs_human" and str(watch.get("status")) != "needs_human":
                try:
                    client.add_labels(repo, number, [sync_rules.NEEDS_HUMAN_LABEL])
                except Exception as exc:
                    logger.debug("[prs] label add failed: %s", exc)
            if settings.get("mode") != "off":
                for trigger in plan.triggers:
                    queue.enqueue(
                        captain="prs", command="address",
                        payload={
                            "repo": repo, "number": number, "workspace_id": self.workspace_id,
                            "org_id": self.config.get("org_id"), "trigger": trigger["trigger"],
                            "review_feedback": trigger["review_feedback"], "fetch_comments": False,
                        },
                        session_id=f"pr:{repo}#{number}", enforce_single_active_session=True,
                    )
                    summary["triggers"] += 1
            if plan.notes:
                emit_job_event(f"{repo}#{number}: " + "; ".join(plan.notes), event_type="info", status="running")
        emit_job_event(
            f"Synced {summary['checked']} pull request(s): {summary['triggers']} to address, {summary['merged']} merged, {summary['closed']} closed, {summary['skipped']} not due",
            event_type="progress", status="completed", data=summary,
        )
        return summary

    def _bookkeep(self, item: Dict[str, Any]) -> None:
        origin = item.get("origin") or {}
        finding_id = origin.get("finding_id")
        hook = origins.status_hook(origin.get("captain"))
        if finding_id and hook is not None:
            try:
                hook(str(finding_id), "Resolved" if item.get("kind") == "merged" else "Review declined", config=self.config, workspace_id=self.workspace_id)
            except Exception as exc:
                logger.debug("[prs] finding status update failed: %s", exc)

    # ── replies ────────────────────────────────────────────────────────────

    def _tell_reviewers(self, client: GitHubPRClient, repo: str, number: int, actionable, ignored, feedback, result, entry) -> None:
        outcome = entry["outcome"]
        sha = str(result.get("commit_sha") or "")[:7]
        for comment in ignored:
            if comment.get("source") == "review_comment" and isinstance(comment.get("id"), int):
                client.reply_to_review_comment(repo, number, comment["id"], f"Thanks — flagged for a maintainer to look at.\n\n{COMPASS_MARKER}")
        if outcome == "pushed":
            for comment in actionable:
                if comment.get("source") == "review_comment" and isinstance(comment.get("id"), int):
                    blocked = next((b for b in (result.get("blockers") or []) if str(comment["id"]) in b), None)
                    text = f"Not changed: {blocked}" if blocked else f"Addressed in {sha}."
                    client.reply_to_review_comment(repo, number, comment["id"], f"{text}\n\n{COMPASS_MARKER}")
            checks = ", ".join(f"{c['name']} ✓" for c in ((result.get("guard") or {}).get("checks") or []) if c.get("ok"))
            body = f"Pushed {sha} addressing {len(actionable)} request(s). Guard: {checks or 'ok'}."
            if result.get("blockers"):
                body += "\n\nNot done:\n" + "\n".join(f"- {b}" for b in result["blockers"])
            client.create_issue_comment(repo, number, body + f"\n\n{COMPASS_MARKER}")
            client.request_reviewers(repo, number, feedback.get("requested_changes_by") or [])
        elif outcome == "refused":
            reason = str(result.get("reason") or "")
            why = _REASON_TEXT.get(reason, reason)
            detail = ""
            for check in ((result.get("guard") or {}).get("checks") or []):
                if not check.get("ok") and check.get("detail"):
                    detail = f" ({check['detail']})"
                    break
            invariants = "\n".join(f"- {describe_invariant(i)}" for i in ((self.registry.get(repo, number) or {}).get("intent") or {}).get("invariants") or [] if isinstance(i, dict))
            body = f"Compass did not push this revision: {why}{detail}.\n\nThis pull request exists to: {(self.registry.get(repo, number) or {}).get('intent', {}).get('goal')}"
            if invariants:
                body += "\n" + invariants
            body += "\n\nA maintainer can take it from here, or change the request."
            client.create_issue_comment(repo, number, body + f"\n\n{COMPASS_MARKER}")
        elif str(result.get("reason")) == "no_revision_runner":
            # The open watcher without a runner: say so plainly, once, and hand over.
            client.create_issue_comment(repo, number, f"Compass has read this request but {_REASON_TEXT['no_revision_runner']}; flagged for a maintainer.\n\n{COMPASS_MARKER}")
            self._flag_human(client, repo, number, "no_revision_runner", {})
        else:
            client.create_issue_comment(repo, number, f"Compass could not revise this pull request: {_REASON_TEXT.get(str(result.get('reason')), result.get('reason'))}. {_short(result.get('error'), 200)}\n\n{COMPASS_MARKER}")
