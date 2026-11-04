"""Jobs run inline are recorded in the job store, so /api/jobs and the dashboard see them."""

import importlib

import pytest

from compass_core import dispatch, queue


@pytest.fixture
def local_store(monkeypatch, tmp_path):
    monkeypatch.setenv("BEAMREACH_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("CAPTAIN_JOBS_TABLE", "")
    local_state = importlib.import_module("compass_core.local_state")
    jobs = importlib.import_module("compass_core.jobs")
    state = local_state.LocalStateStore(path=str(tmp_path / "state.json"))
    monkeypatch.setattr(local_state, "local_state_store", state)
    monkeypatch.setattr(jobs, "local_state_store", state)
    return jobs.CaptainJobStore()


def test_inline_jobs_are_recorded_completed_and_failed(local_store, monkeypatch):
    monkeypatch.setattr(dispatch, "_registry", {})
    dispatch.register("x", {"ok": lambda p: {"n": 1}, "boom": lambda p: (_ for _ in ()).throw(ValueError("boom"))})
    q = queue.InlineQueue(dispatch.dispatch, store=local_store)

    done = q.enqueue(captain="x", command="ok", payload={"workspace_id": "ws"})
    stored = q.get_job(done["job_id"], workspace_id="ws")
    assert stored["status"] == "completed" and stored["result_summary"] == {"n": 1}
    assert stored["captain"] == "x" and stored["command"] == "ok"

    with pytest.raises(ValueError):
        q.enqueue(captain="x", command="boom", payload={"workspace_id": "ws"})
    failed = [j for j in q.list_jobs(workspace_id="ws") if j["command"] == "boom"]
    assert failed and failed[0]["status"] == "failed" and "boom" in failed[0]["error"]


def test_store_for_needs_a_jobs_table():
    from compass_core.jobs import JOB_ID_INDEX, store_for

    assert store_for({"dynamodb_config": {}}) is None
    store = store_for({"dynamodb_config": {"jobs": "compass_jobs_local"}})
    assert store.table_name == "compass_jobs_local" and store._job_id_index == JOB_ID_INDEX


def test_init_tables_specs_include_the_jobs_table():
    from compass_core.commands import table_specs

    spec = {s["TableName"]: s for s in table_specs({"dynamodb_config": {"jobs": "compass_jobs_local", "map": "m"}})}["compass_jobs_local"]
    assert spec["GlobalSecondaryIndexes"][0]["IndexName"] == "JobIdIndex"
    assert {a["AttributeName"]: a["AttributeType"] for a in spec["AttributeDefinitions"]}["created_at"] == "N"


def test_heartbeat_keeps_a_running_job_alive(local_store):
    import time

    from compass_core.jobs import heartbeat

    job = local_store.create_job(captain="mapper", command="run", payload={}, queue="q", workspace_id="ws")
    local_store.claim_running(job["job_id"], workspace_id="ws", worker_id="t")
    before = local_store.get_job(job["job_id"], workspace_id="ws").get("heartbeat_at") or 0
    time.sleep(0.01)
    with heartbeat(local_store, job["job_id"], workspace_id="ws", interval=0.05):
        time.sleep(0.2)
    after = local_store.get_job(job["job_id"], workspace_id="ws")
    assert after["status"] == "running" and int(after["heartbeat_at"]) > int(before)


def test_the_newest_jobs_are_listed_whatever_their_ids(monkeypatch):
    """Job keys are JOB#<uuid>, so key order is not time order: a listing that read one
    page of `limit` items lost the newest jobs once a workspace had more than `limit`."""
    import boto3
    from moto import mock_aws

    from compass_core import commands
    from compass_core.jobs import store_for

    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    config = {"dynamodb_config": {"workspace_id": "ws", "jobs": "compass_jobs_ws"}}
    with mock_aws():
        commands.create_tables(config, client=boto3.client("dynamodb", region_name="us-east-1"))
        store = store_for(config)
        table = boto3.resource("dynamodb", region_name="us-east-1").Table("compass_jobs_ws")
        for n in range(40):
            job_id = f"{(n * 7919) % 100:02d}-job-{n}"  # ids whose order has nothing to do with age
            table.put_item(Item={"pk": "WORKSPACE#ws", "sk": f"JOB#{job_id}", "job_id": job_id, "workspace_id": "ws",
                                 "captain": "prs", "command": "sync", "status": "completed", "created_at": 1000 + n, "updated_at": 1000 + n})
        newest = store.list_jobs(limit=5, workspace_id="ws")
        assert [j["created_at"] for j in newest] == [1039, 1038, 1037, 1036, 1035]


def test_a_payload_with_a_float_lands_on_the_table(monkeypatch):
    """An injected advisory carries a CVSS score; DynamoDB takes no float, and the API answered 500."""
    from decimal import Decimal

    import boto3
    from moto import mock_aws

    from compass_core import commands
    from compass_core.jobs import store_for

    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    config = {"dynamodb_config": {"workspace_id": "ws", "jobs": "compass_jobs_ws"}}
    with mock_aws():
        commands.create_tables(config, client=boto3.client("dynamodb", region_name="us-east-1"))
        store = store_for(config)
        job = store.create_job(captain="patch", command="feed_inject", payload={"workspace_id": "ws", "advisory": {"id": "CVE-2026-1", "cvss": 9.2, "affected": [{"ranges": [0.5]}]}},
                               queue="compass", workspace_id="ws")
        stored = store.get_job(job["job_id"], workspace_id="ws")
        assert stored["payload"]["advisory"]["cvss"] == Decimal("9.2") and stored["payload"]["advisory"]["affected"][0]["ranges"] == [Decimal("0.5")]


class _Table:
    """The two calls the store makes, and a refusal when asked for one."""

    def __init__(self, refuse=False):
        self.items, self.refuse = [], refuse

    def put_item(self, Item):
        if self.refuse:
            from botocore.exceptions import ClientError

            raise ClientError({"Error": {"Code": "ValidationException", "Message": "Item size has exceeded the maximum allowed size"}}, "PutItem")
        self.items.append(Item)


def test_a_job_keeps_its_first_and_latest_events_only(local_store):
    from compass_core.jobs import MAX_JOB_EVENTS

    local_store._table = table = _Table()
    events = [{"timestamp": n, "message": str(n)} for n in range(MAX_JOB_EVENTS * 3)]
    local_store._persist_item({"job_id": "j", "workspace_id": "ws", "events": events})
    kept = table.items[0]["events"]
    assert len(kept) == MAX_JOB_EVENTS
    assert kept[0]["message"] == "0" and kept[-1]["message"] == str(MAX_JOB_EVENTS * 3 - 1)


def test_an_item_the_table_refuses_does_not_take_the_table_away(local_store):
    local_store._table = table = _Table(refuse=True)
    local_store._persist_item({"job_id": "big", "workspace_id": "ws"})
    assert local_store._table is table, "the next job can still be written"


def test_the_local_store_takes_what_dynamodb_returned(local_store):
    from decimal import Decimal

    stored = local_store._persist_item({"job_id": "d", "workspace_id": "ws", "updated_at": Decimal("1793180254709"), "score": Decimal("7.5")})
    assert stored["updated_at"] == 1793180254709 and stored["score"] == 7.5
