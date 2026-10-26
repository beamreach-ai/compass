"""The inbox derives one item per thing that needs a person, and the item leaves when
the state changes (docs/specs/one-inbox.md §3.3, §5)."""

from __future__ import annotations

from decimal import Decimal

import boto3
import pytest
from moto import mock_aws

from captains.honeypot import incidents
from compass_core import inbox
from compass_core.access_audit import FINDING_PREFIX
from compass_core.keys import grant_sort_key, honeytoken_sort_key, workspace_pk

PK = workspace_pk("ws-a")
T0 = 1_792_670_000_000
CONFIG = {"aws": {"roles": {"read": "arn:aws:iam::123456789012:role/service/svc-inventory-reader-x", "act": None}, "account_id": "123456789012",
                  "region": "us-east-1", "control": {"alert_queues": {"us-east-1": {"arn": "arn:aws:sqs:us-east-1:123456789012:compass-alerts"}}}},
          "github": {"installation_id": 42}}


@pytest.fixture
def table(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    inbox.reset_for_tests()
    with mock_aws():
        yield boto3.resource("dynamodb", region_name="us-east-1").create_table(
            TableName="items",
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}, {"AttributeName": "sk", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
    inbox.reset_for_tests()


def ctx(table, *, config=CONFIG, jobs=(), pr_records=(), update=None, model=True, now_ms=T0 + 60_000) -> inbox.Context:
    return inbox.Context(config=config, workspace_id="ws-a", pk=PK, table=table, jobs=lambda: list(jobs), pr_records=lambda: list(pr_records),
                         update=lambda: dict(update or {}), model_available=lambda: model, now_ms=now_ms)


def feed(c: inbox.Context, **filters):
    return inbox.items(c, fresh=True, **filters)


def by_kind(c: inbox.Context, kind: str):
    return [i for i in feed(c)["items"] if i["kind"] == kind]


def _incident(table, n=1, pending=True):
    alert = {"alert_id": f"a{n}", "honeytoken_id": "tok", "source_ip": "203.0.113.9", "source_task_id": f"t{n}", "triggered_at": "2026-10-22T12:00:00Z"}
    incident, _ = incidents.attach_alert(table, PK, alert, placement={"service_arn": "arn:aws:ecs:us-east-1:1:service/prod/payments-api"}, now_ms=T0 + n)
    action = {"plan": [{"action_id": "act-1", "action_type": "ecs_restart_task", "breakdown": {"blast_radius": 0.4}, "notes": ["env=prod:+1"]}],
              "decisions": [], "context": {"service_arn": "arn:aws:ecs:us-east-1:1:service/prod/payments-api", "task_arn": "arn:aws:ecs:us-east-1:1:task/x"},
              "pending": [{"action_id": "act-1", "action_type": "ecs_restart_task", "risk": 3, "reason": "mode is approve"}] if pending else []}
    incidents.mark(table, PK, incident["incident_id"], action=action)
    return incident["incident_id"]


def test_a_parked_decoy_response_is_an_urgent_approval_until_run_or_declined(table):
    incident_id = _incident(table)
    [item] = by_kind(ctx(table), "decoy.approval")
    assert item["id"] == f"decoy.approval:{incident_id}:ecs_restart_task"
    assert item["verb"] == "approve" and item["severity"] == "urgent" and item["tab"] == "honeypots"
    assert item["since"] == T0 + 1
    run, decline, open_ = item["actions"]
    assert run == {"key": "run", "label": "Run", "method": "POST", "path": f"/api/honeypots/incidents/{incident_id}/approve",
                   "body": {"action_id": "act-1"}, "scope": "operate", "confirm": "Runs ecs_restart_task on payments-api (risk 3)."}
    assert decline["path"].endswith("/decline") and open_["href"].endswith(incident_id)
    assert item["detail"]["risk"] == {"score": 3, "breakdown": {"blast_radius": 0.4}, "notes": ["env=prod:+1"]}
    assert item["detail"]["incident"]["source_ip"] == "203.0.113.9"
    # declining drops the pending entry and the item
    from captains.honeypot.respond import decline_pending_action

    result = decline_pending_action(table=table, pk=PK, incident_id=incident_id, action_id="act-1", declined_by="ilia")
    assert result["pending"] == []
    assert by_kind(ctx(table), "decoy.approval") == []
    decisions = incidents.get_incident(table, PK, incident_id)["action"]["decisions"]
    assert decisions[-1]["decision"] == "declined" and decisions[-1]["reason"] == "declined by ilia"
    # a closed incident with a pending entry is not an item either
    _incident(table, 2)
    incidents.mark(table, PK, incidents.list_incidents(table, PK)[0]["incident_id"], status="closed")
    assert by_kind(ctx(table), "decoy.approval") == []


def test_a_proposed_grant_asks_for_approval_and_an_open_access_pr_asks_for_a_merge(table):
    table.put_item(Item={"pk": PK, "sk": grant_sort_key("honeypot", "g1"), "action_id": "g1", "captain": "honeypot", "action_type": "iam_deactivate_key",
                         "grant_status": "proposed", "mode": "approve", "created_at": "2026-10-22T10:00:00Z", "created_by": "ilia", "scope": {}})
    [item] = by_kind(ctx(table), "grant.proposed")
    assert item["id"] == "grant.proposed:g1" and item["verb"] == "approve" and item["tab"] == "settings"
    assert item["since"] == inbox._ms("2026-10-22T10:00:00Z")
    assert [a["key"] for a in item["actions"]] == ["accept", "decline", "open"]
    assert item["actions"][0]["path"] == "/api/access/grants/g1/accept" and item["actions"][0]["scope"] == "admin"
    table.update_item(Key={"pk": PK, "sk": grant_sort_key("honeypot", "g1")}, UpdateExpression="SET grant_status = :s, grant_pr = :u",
                      ExpressionAttributeValues={":s": "pr_open", ":u": "https://github.com/acme/infra/pull/9"})
    assert by_kind(ctx(table), "grant.proposed") == []
    [waiting] = by_kind(ctx(table), "grant.waiting")
    assert waiting["verb"] == "merge" and waiting["severity"] == "info"
    assert waiting["actions"][0] == {"key": "open", "label": "Open pull request", "href": "https://github.com/acme/infra/pull/9"}
    table.update_item(Key={"pk": PK, "sk": grant_sort_key("honeypot", "g1")}, UpdateExpression="SET grant_status = :s", ExpressionAttributeValues={":s": "armed"})
    assert by_kind(ctx(table), "grant.waiting") == []


def test_a_decoy_waits_for_its_merge_then_for_its_arm(table):
    table.put_item(Item={"pk": PK, "sk": honeytoken_sort_key("tok-1"), "honeytoken_id": "tok-1", "name": "svc_nightly_backup", "honeytoken_class": "aws_credential",
                         "status": "pending_pr", "created_at": T0, "placement": {"pr_url": "https://github.com/acme/infra/pull/3", "module_path": "services/backup"}})
    record = {"repo": "acme/infra", "number": 3, "url": "https://github.com/acme/infra/pull/3", "type": "honeypot_plant", "state": "open",
              "origin": {"captain": "honeypot", "honeytoken_id": "tok-1"}, "created_at": T0 + 5, "updated_at": T0 + 5}
    [item] = by_kind(ctx(table, pr_records=[record]), "decoy.waiting")
    assert item["verb"] == "merge" and item["since"] == T0 + 5 and item["actions"][0]["href"] == record["url"]
    merged = {**record, "state": "merged", "updated_at": T0 + 9}
    c = ctx(table, pr_records=[merged])
    assert by_kind(c, "decoy.waiting") == []
    [arm] = by_kind(c, "decoy.arm")
    assert arm["verb"] == "fix" and arm["since"] == T0 + 9
    assert arm["actions"][0] == {"key": "arm", "label": "Arm", "method": "POST", "path": "/api/honeypots/tokens/tok-1/arm", "body": {}, "scope": "operate"}
    table.update_item(Key={"pk": PK, "sk": honeytoken_sort_key("tok-1")}, UpdateExpression="SET #s = :s", ExpressionAttributeNames={"#s": "status"},
                      ExpressionAttributeValues={":s": "armed"})
    assert by_kind(ctx(table, pr_records=[merged]), "decoy.arm") == []


def test_pull_requests_with_a_pending_revision_or_needing_a_human(table):
    base = {"repo": "acme/infra", "number": 7, "url": "https://github.com/acme/infra/pull/7", "type": "iac_import", "state": "open", "updated_at": T0,
            "origin": {}, "watch": {"status": "watching"}, "pending_revision": None, "decisions": []}
    revision = {**base, "pending_revision": {"created_at": T0 + 100, "feedback": [{"body": "rename the module"}], "summary": "rename module x to y"}}
    [item] = by_kind(ctx(table, pr_records=[revision]), "pr.revision")
    assert item["id"] == "pr.revision:acme/infra#7" and item["verb"] == "approve" and item["since"] == T0 + 100
    assert item["actions"][0]["path"] == "/api/prs/acme/infra/7/revision/approve"
    assert item["detail"]["comments"] == [{"body": "rename the module"}]
    stuck = {**base, "watch": {"status": "needs_human", "reason": "scope_expanded", "since": T0 + 200}}
    [item] = by_kind(ctx(table, pr_records=[stuck]), "pr.needs_human")
    assert item["verb"] == "fix" and item["since"] == T0 + 200
    assert item["summary"] == "the change would reach outside the files this pull request touches"
    assert [a["key"] for a in item["actions"]] == ["open", "resume", "pause"]
    assert by_kind(ctx(table, pr_records=[{**stuck, "state": "merged"}]), "pr.needs_human") == []
    assert by_kind(ctx(table, pr_records=[base]), "pr.needs_human") == []


def test_the_newest_failed_job_per_command_is_urgent_until_a_newer_one_completes_or_it_is_acknowledged(table):
    jobs = [{"job_id": "j3", "captain": "mapper", "command": "run", "status": "failed", "error": "AccessDenied on ec2:DescribeInstances", "updated_at": Decimal(T0 + 300),  # as the jobs table returns it
             "events": [{"message": f"e{i}"} for i in range(8)]},
            {"job_id": "j2", "captain": "mapper", "command": "run", "status": "failed", "error": "older", "updated_at": T0 + 200},
            {"job_id": "j1", "captain": "prs", "command": "sync", "status": "dead", "updated_at": T0 + 100}]
    items = by_kind(ctx(table, jobs=jobs), "job.failed")
    assert [i["id"] for i in items] == ["job.failed:j3", "job.failed:j1"] and items[0]["since"] == T0 + 300
    assert items[0]["severity"] == "urgent" and items[0]["title"] == "mapper.run failed" and items[0]["summary"].startswith("AccessDenied")
    assert len(items[0]["detail"]["events"]) == 5
    assert items[1]["title"] == "prs.sync died"
    assert [a["key"] for a in items[0]["actions"]] == ["retry", "acknowledge", "open"]
    assert items[0]["actions"][0]["path"] == "/api/jobs/j3/restart" and items[0]["actions"][1]["snooze"] is True
    # a newer completed run of the same command clears it
    newer = [{"job_id": "j4", "captain": "mapper", "command": "run", "status": "completed", "updated_at": T0 + 400}] + jobs
    assert [i["id"] for i in by_kind(ctx(table, jobs=newer), "job.failed")] == ["job.failed:j1"]
    # acknowledging hides it; a later failure is a new item
    inbox.snooze(table, PK, "job.failed:j1", by="ilia", now_ms=T0 + 500)
    assert by_kind(ctx(table, jobs=newer), "job.failed") == []
    shown = inbox.items(ctx(table, jobs=newer), include_snoozed=True, fresh=True)
    hidden = [i for i in shown["items"] if i["kind"] == "job.failed"]
    assert hidden[0]["snooze"]["snoozed_by"] == "ilia" and shown["counts"]["total"] == 0
    again = [{"job_id": "j5", "captain": "prs", "command": "sync", "status": "failed", "updated_at": T0 + 600}] + newer
    assert [i["id"] for i in by_kind(ctx(table, jobs=again), "job.failed")] == ["job.failed:j5"]
    inbox.unsnooze(table, PK, "job.failed:j1")
    assert inbox.snoozes(table, PK) == {}


def test_an_access_finding_is_an_item_until_the_next_audit_drops_it(table):
    table.put_item(Item={"pk": PK, "sk": f"{FINDING_PREFIX}f1", "finding_id": "f1", "kind": "unexplained", "role": "act", "detail": "a statement nobody granted",
                         "first_seen": "2026-10-21T09:00:00Z", "closed_at": None})
    table.put_item(Item={"pk": PK, "sk": f"{FINDING_PREFIX}f2", "finding_id": "f2", "kind": "missing", "role": "read", "detail": "ecr:Describe* is not in the policy",
                         "first_seen": "2026-10-22T09:00:00Z", "closed_at": None})
    items = by_kind(ctx(table), "access.finding")
    assert [(i["id"], i["severity"]) for i in items] == [("access.finding:f2", "normal"), ("access.finding:f1", "urgent")]
    assert items[1]["title"] == "Unexplained on the act role" and items[1]["actions"][0]["path"] == "/api/access/pr"
    table.update_item(Key={"pk": PK, "sk": f"{FINDING_PREFIX}f1"}, UpdateExpression="SET closed_at = :a", ExpressionAttributeValues={":a": "2026-10-22T10:00:00Z"})
    assert [i["id"] for i in by_kind(ctx(table), "access.finding")] == ["access.finding:f2"]


def test_an_available_update_is_a_later_item_dismissed_until_the_next_release(table):
    update = {"available": True, "latest": "0.9.0", "running": "0.8.0", "apply": "updater", "direct": True,
              "release": {"version": "0.9.0", "date": "2026-10-23"}, "release_notes": [{"version": "0.9.0", "title": "The patching captain"}]}
    [item] = by_kind(ctx(table, update=update), "update.available")
    assert item["id"] == "update.available:0.9.0" and item["verb"] == "later" and item["severity"] == "info"
    assert item["summary"] == "The patching captain" and item["since"] == inbox._ms("2026-10-23")
    assert item["actions"][0]["path"] == "/api/updates/apply" and item["actions"][0]["body"] == {"version": "0.9.0"}
    [manual] = by_kind(ctx(table, update={**update, "apply": "manual"}), "update.available")
    assert manual["actions"][0] == {"key": "open", "label": "Open updates", "href": "/workspace?tab=settings#settings-updates"}
    inbox.snooze(table, PK, "update.available:0.9.0")
    assert by_kind(ctx(table, update=update), "update.available") == []
    [newer] = by_kind(ctx(table, update={**update, "latest": "0.9.1"}), "update.available")
    assert newer["id"] == "update.available:0.9.1"
    assert by_kind(ctx(table, update={**update, "available": False}), "update.available") == []


def test_an_incomplete_setup_lists_what_is_missing_and_the_model_can_be_waived(table):
    items = by_kind(ctx(table, config={}, model=False), "setup.incomplete")
    assert [(i["subject"]["what"], i["severity"]) for i in items] == [("account", "urgent"), ("github", "urgent"), ("model", "info")]
    assert items[0]["actions"] == [{"key": "open", "label": "Open settings", "href": "/workspace?tab=settings#settings-integrations"}]
    assert items[2]["actions"][1]["snooze"] is True
    no_queue = {**CONFIG, "aws": {**CONFIG["aws"], "control": {}}}
    assert [i["subject"]["what"] for i in by_kind(ctx(table, config=no_queue, model=False), "setup.incomplete")] == ["alerts", "model"]
    assert by_kind(ctx(table, config=CONFIG, model=True), "setup.incomplete") == []
    inbox.snooze(table, PK, "setup.incomplete:model")
    assert by_kind(ctx(table, config=CONFIG, model=False), "setup.incomplete") == []


def test_the_feed_is_newest_first_counted_filtered_and_cached_for_five_seconds(table, monkeypatch):
    _incident(table)
    jobs = [{"job_id": "j1", "captain": "mapper", "command": "run", "status": "failed", "error": "x", "updated_at": T0 + 50_000}]
    c = ctx(table, jobs=jobs, update={"available": True, "latest": "0.9.0", "release": {"date": "2026-10-23"}, "apply": "manual"})
    first = inbox.items(c)
    assert [i["kind"] for i in first["items"]] == ["update.available", "job.failed", "decoy.approval"]
    assert first["counts"] == {"total": 3, "urgent": 2, "by_tab": {"settings": 1, "queue": 1, "honeypots": 1},
                               "by_verb": {"approve": 1, "fix": 1, "merge": 0, "later": 1}}
    assert first["generated_at"] == T0 + 60_000
    # a second call within the window is the same computation, whatever changed meanwhile
    c.jobs = lambda: []
    second = inbox.items(c, tab="queue")
    assert second["generated_at"] == first["generated_at"] and [i["kind"] for i in second["items"]] == ["job.failed"]
    assert inbox.items(c, verb="approve")["items"][0]["kind"] == "decoy.approval"
    assert inbox.items(c, kind="update.available")["items"][0]["id"] == "update.available:0.9.0"
    now = [1_000.0]
    monkeypatch.setattr(inbox.time, "time", lambda: now[0])
    inbox.items(c, fresh=True)
    now[0] += inbox.CACHE_SECONDS + 1
    assert [i["kind"] for i in inbox.items(c)["items"]] == ["update.available", "decoy.approval"]
    assert inbox.status_line(first["counts"]) == "Needs you: 3 (2 urgent): 1 to approve, 1 to fix, 1 later"
    assert inbox.status_line({"total": 0}) == "Needs you: nothing"


def test_a_registered_source_contributes_items_and_a_failing_one_is_skipped(table, caplog):
    def finops(c: inbox.Context):
        return [inbox.Item(id="finops.fix:rds-1", kind="finops.fix", verb="approve", severity="normal", captain="finops", tab="finops",
                           title="Downsize db-prod", summary="saves $400/month", since=T0 + 1)]

    def broken(c: inbox.Context):
        raise RuntimeError("no table")

    inbox.register_source("finops", finops)
    inbox.register_source("broken", broken)
    result = feed(ctx(table))
    assert [i["id"] for i in result["items"]] == ["finops.fix:rds-1"] and result["counts"]["by_tab"] == {"finops": 1}
    assert "inbox source broken failed: no table" in caplog.text
    inbox.register_source("finops", None)
    inbox.register_source("broken", None)
    assert feed(ctx(table))["items"] == []


def test_items_survive_a_missing_items_table(table):
    c = ctx(None, config={}, model=False)
    kinds = {i["kind"] for i in feed(c)["items"]}
    assert kinds == {"setup.incomplete"}
