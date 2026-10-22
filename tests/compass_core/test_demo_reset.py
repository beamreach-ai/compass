"""`compass demo reset` (docs/demo.md): the visitor's pull request closed on GitHub and its
branch deleted, its decoy and grant retired with no pull request, the seeded pull request
recreated with the reviewer's request, old jobs pruned, the trips and tours cleared, the
world seeded again as a no-op, a reset row with the counts; a second run changes nothing
and reports. On moto, with the e2e fake GitHub served in-process; `demo status` reads it back."""

from __future__ import annotations

import socket
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict

import boto3
import httpx
import pytest
from moto import mock_aws

import captains.honeypot.actions  # noqa: F401 - registers the response actions the grant names
from compass_core import config as config_module
from compass_core import demo, demo_reset, grants
from compass_core.github import client as github_client
from compass_core.github_prs import commit_files, ensure_branch, open_or_update_pr
from compass_core.keys import demo_cooldown_sort_key, demo_tour_sort_key, demo_visitor_sort_key, honeytoken_sort_key, workspace_pk
from compass_core.registry import PRRegistry

WS = "ws-demo"
PK = workspace_pk(WS)
REPO = "acme/infra"


@pytest.fixture(scope="module")
def fake_github():
    """The e2e suite's fake GitHub, on a port of its own, for this module."""
    import uvicorn

    from e2e.fakes.app import app

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    deadline = time.time() + 15
    while time.time() < deadline:
        try:
            if httpx.get(f"{base}/health", timeout=1).status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.2)
    yield base
    server.should_exit = True
    thread.join(timeout=5)


@pytest.fixture
def env(fake_github, monkeypatch, tmp_path):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    monkeypatch.setenv("APP_MODE", "demo")
    monkeypatch.setenv("GITHUB_API_URL", f"{fake_github}/github")
    monkeypatch.delenv("COMPASS_MAP_STORE", raising=False)
    httpx.post(f"{fake_github}/_control/reset").raise_for_status()
    httpx.put(f"{fake_github}/_control/repos/acme/infra", json={"files": {"README.md": "infra\n", "environments/prod/main.tf": "# prod\n"}}).raise_for_status()
    config: Dict[str, Any] = {
        "workspace_id": WS, "org_id": "acme",
        "dynamodb_config": {"workspace_id": WS, "items": "compass_items_ws", "map": "compass_map_ws", "jobs": "compass_jobs_ws"},
        "aws": {"account_id": "123456789012", "region": "us-east-1"},
        "github": {"token": "ghp_test", "iac_repo": REPO},
        "map": {"store": "sqlite", "path": str(tmp_path / "map.sqlite")},
        "demo": {"world": {"resources": 60, "seed": 3}, "visitor_repo": REPO, "trippable": [], "seeded_pr": {"reviewer": "demo-reviewer", "reviewer_token": "ghp_reviewer"}},
    }
    monkeypatch.setattr(config_module, "_loader", lambda ws: dict(config))
    with mock_aws():
        from compass_core.commands import create_tables

        session = boto3.Session(region_name="us-east-1")
        create_tables(config, client=session.client("dynamodb"))
        table = session.resource("dynamodb").Table("compass_items_ws")
        yield config, table, session


def _visitor_pr(config: Dict[str, Any]) -> Dict[str, Any]:
    """What a visitor's plant leaves behind: a pull request on the repository, its record, a decoy row, a grant row."""
    gh_repo = github_client("ghp_test").get_repo(REPO)
    ensure_branch(gh_repo, "beamreach/honeypot/svc-backup", "main")
    commit_files(gh_repo, "beamreach/honeypot/svc-backup", {"services/svc_backup/main.tf": "# decoy\n"}, add_message="plant", update_message="plant")
    published = open_or_update_pr(gh_repo, branch="beamreach/honeypot/svc-backup", base_branch="main", title="Plant a decoy", body="Opened from the demo by a visitor.")
    registry = PRRegistry.from_config(config, workspace_id=WS)
    registry.record(url=published.url, pr_type="honeypot_plant", origin={"captain": "honeypot", "command": "plant", "honeytoken_id": "tok-v", "user_id": "visitor-1"},
                    intent={"goal": "plant"}, branch="beamreach/honeypot/svc-backup", base_branch="main", title="Plant a decoy")
    return {"url": published.url, "number": published.number}


def _pulls(fake_github: str, state: str = "open"):
    return httpx.get(f"{fake_github}/github/repos/acme/infra/pulls", params={"state": state}, headers={"Authorization": "token ghp_test"}).json()


def test_the_reset_brings_the_golden_world_back_and_a_second_run_changes_nothing(env, fake_github):
    config, table, session = env
    demo.seed({"workspace_id": WS})
    visitor = _visitor_pr(config)
    table.put_item(Item={"pk": PK, "sk": honeytoken_sort_key("tok-v"), "honeytoken_id": "tok-v", "honeytoken_class": "aws_credential", "name": "svc-backup",
                         "status": "pending_merge", "material": {}, "trigger_count": 0, "created_at": 1, "updated_at": 1, "pr_url": visitor["url"]})
    grant = grants.propose(table, PK, captain="honeypot", action_type="iam_deactivate_key", scope={"user_path_prefixes": ["/service/"]}, mode="autopilot")
    grants.accept(table, PK, grant["action_id"], config=config, actor="visitor-1", enqueue=lambda **kw: None)
    row = grants.get_grant(table, PK, grant["action_id"])
    row["grant_pr"] = visitor["url"]
    grants._put(table, row)
    now = demo.epoch_ms()
    demo.claim_trip(table, PK, "tok-1", user_id="visitor-1", cooldown_seconds=300, now_ms=now)
    demo.record_visitor_trip(table, PK, "visitor-1", now_ms=now)
    demo.mark_tour_step(table, PK, "visitor-1", "map")
    jobs = session.resource("dynamodb").Table("compass_jobs_ws")
    old, recent = now - 9 * 86_400_000, now - 86_400_000
    for job_id, created in (("job-old", old), ("job-recent", recent)):
        jobs.put_item(Item={"pk": PK, "sk": f"JOB#{job_id}", "job_id": job_id, "workspace_id": WS, "captain": "mapper", "command": "run", "status": "completed",
                            "created_at": created, "updated_at": created, "events": []})

    calls: Dict[str, Any] = {"decoys": 0, "rescan": 0, "lines": []}

    def decoys():
        calls["decoys"] += 1
        return {"incidents_closed": 2, "rolled_back": 1, "rearmed": ["tok-1"], "failed": [], "steady_state": None}

    def rescan():
        calls["rescan"] += 1
        return "job-rescan"

    result = demo_reset.reset(config, workspace_id=WS, decoys=decoys, rescan=rescan, notify=lambda line: calls["lines"].append(line))
    assert result["decoys"]["incidents_closed"] == 2 and calls["rescan"] == 1 and result["rescan_job_id"] == "job-rescan"
    assert result["pull_requests"] == {"closed": 1, "branches_deleted": 1, "decoys_retired": 1, "grants_retired": 1, "failed": []}
    closed = next(p for p in _pulls(fake_github, "closed") if p["number"] == visitor["number"])
    assert closed["state"] == "closed" and closed["closed_at"]
    assert table.get_item(Key={"pk": PK, "sk": honeytoken_sort_key("tok-v")})["Item"]["status"] == "decommissioned"
    assert grants.get_grant(table, PK, grant["action_id"])["grant_status"] == "disarmed"
    registry = PRRegistry.from_config(config, workspace_id=WS)
    assert registry.get(REPO, visitor["number"])["state"] == "closed"

    seeded = result["seeded_pr"]
    assert seeded["number"] and seeded["reviewer"] == "demo-reviewer"
    open_prs = _pulls(fake_github)
    assert [p["number"] for p in open_prs] == [seeded["number"]] and "demo" in [label["name"] for label in open_prs[0]["labels"]]
    reviews = httpx.get(f"{fake_github}/github/repos/acme/infra/pulls/{seeded['number']}/reviews", headers={"Authorization": "token ghp_test"}).json()
    assert reviews and reviews[0]["state"] == "CHANGES_REQUESTED" and "queue_visibility_timeout" in reviews[0]["body"]
    recorded = registry.get(REPO, seeded["number"])
    assert recorded["type"] == "demo_seeded" and recorded["origin"]["seeded"] is True and recorded["branch"] == demo_reset.SEEDED_BRANCH

    assert result["jobs_pruned"] == 1 and jobs.get_item(Key={"pk": PK, "sk": "JOB#job-recent"}).get("Item") and not jobs.get_item(Key={"pk": PK, "sk": "JOB#job-old"}).get("Item")
    assert result["rows_cleared"] == 3
    for sk in (demo_cooldown_sort_key("tok-1"), demo_visitor_sort_key("visitor-1"), demo_tour_sort_key("visitor-1")):
        assert not table.get_item(Key={"pk": PK, "sk": sk}).get("Item")
    assert result["world"]["unchanged"] is True and result["world"]["scope"] == "demo:seed:3"
    assert calls["lines"] == [result["summary"]] and "1 visitor pull requests closed" in result["summary"]

    shown = demo_reset.status(config, workspace_id=WS)
    assert shown["last_reset"]["reset_at"] == result["started_at"] and shown["last_reset"]["jobs_pruned"] == 1
    upcoming = datetime.fromisoformat(shown["next_reset"])
    assert upcoming.weekday() == 6 and upcoming.hour == 3 and upcoming.minute == 0 and upcoming > datetime.now(timezone.utc)
    assert shown["visitor_prs_open"] == [] and shown["cooldowns_in_force"] == [] and shown["tour_completions_this_week"] == 0

    # again: the seeded pull request is closed and recreated, nothing else changes
    again = demo_reset.reset(config, workspace_id=WS, decoys=decoys, rescan=rescan)
    assert again["pull_requests"] == {"closed": 1, "branches_deleted": 1, "decoys_retired": 0, "grants_retired": 0, "failed": []}
    assert again["seeded_pr"]["number"] != seeded["number"] and [p["number"] for p in _pulls(fake_github)] == [again["seeded_pr"]["number"]]
    assert again["jobs_pruned"] == 0 and again["rows_cleared"] == 0 and again["world"]["unchanged"] is True


def test_next_run_reads_the_schedule_and_refuses_what_it_does_not(env):
    after = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)  # a Wednesday
    assert demo_reset.next_run("cron(0 3 ? * SUN *)", after=after) == datetime(2026, 10, 25, 3, 0, tzinfo=timezone.utc)
    assert demo_reset.next_run("cron(30 3 ? * WED *)", after=after) == datetime(2026, 10, 28, 3, 30, tzinfo=timezone.utc)
    assert demo_reset.next_run("cron(0 3 ? * 1 *)", after=after) == datetime(2026, 10, 25, 3, 0, tzinfo=timezone.utc)
    assert demo_reset.next_run("rate(1 day)") is None and demo_reset.next_run("cron(0 3 1 * ? *)") is None


def test_outside_demo_mode_the_reset_and_the_status_refuse(env, monkeypatch):
    config, *_ = env
    monkeypatch.setenv("APP_MODE", "production")
    with pytest.raises(ValueError, match="runs only in demo mode"):
        demo_reset.reset(config, workspace_id=WS, decoys=dict, rescan=lambda: None)
    with pytest.raises(ValueError, match="runs only in demo mode"):
        demo_reset.status(config, workspace_id=WS)


def test_without_the_reviewers_token_the_reset_completes_and_says_why_there_is_no_review(env, fake_github):
    """GitHub refuses a review requesting changes from the pull request's own author, so without
    the bot account's token the seeded pull request has no review, and the reset still finishes."""
    config, table, session = env
    config["demo"]["seeded_pr"] = {"reviewer": "demo-reviewer"}
    result = demo_reset.reset(config, workspace_id=WS, decoys=dict, rescan=lambda: None)
    seeded = result["seeded_pr"]
    assert seeded["number"] and "reviewer_token" in seeded["review"] and result["world"]["scope"], result
    reviews = httpx.get(f"{fake_github}/github/repos/acme/infra/pulls/{seeded['number']}/reviews", headers={"Authorization": "token ghp_test"}).json()
    assert reviews == [], "no review from Compass on its own pull request"
