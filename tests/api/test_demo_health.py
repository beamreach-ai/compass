"""GET /healthz/visitor (docs/demo.md): public, 200 while a visitor arriving now would get the demo
the ads promise, 503 naming what is wrong: the tour, no recent pull request (a visitor's within a
day, a probe's within two hours), a job queued longer than a worker takes to pick one up, the
model budget spent, and whatever the product adds."""

from __future__ import annotations

from typing import Any, Dict, List

import boto3
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from moto import mock_aws

from compass_api import extensions
from compass_api.middleware.simple_auth import _PUBLIC_PATHS
from compass_api.routes import demo as demo_routes
from compass_core import demo
from compass_core.keys import workspace_pk

WS = "ws-demo"
PK = workspace_pk(WS)
HOUR = 3_600_000
CONFIG: Dict[str, Any] = {"workspace_id": WS, "org_id": "acme", "dynamodb_config": {"workspace_id": WS, "items": "items", "map": "map"},
                          "aws": {"account_id": "123456789012"}, "demo": {"health": {"probe_users": ["token:probe"]}}}


class _Queue:
    jobs: List[Dict[str, Any]] = []

    def list_jobs(self, limit: int = 100, workspace_id: Any = None) -> List[Dict[str, Any]]:
        return list(self.jobs)


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    monkeypatch.setenv("APP_MODE", "demo")
    with mock_aws():
        table = boto3.resource("dynamodb", region_name="us-east-1").create_table(
            TableName="items",
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}, {"AttributeName": "sk", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        prs: List[Dict[str, Any]] = []
        queue = _Queue()
        queue.jobs = []
        monkeypatch.setattr(demo_routes, "get_actor_ids", lambda request: {"workspace_id": WS, "org_id": "acme", "user_id": None})
        monkeypatch.setattr(demo_routes, "get_workspace_config", lambda request: dict(CONFIG))
        monkeypatch.setattr(demo_routes, "get_workspace_dynamodb_config", lambda request: dict(CONFIG["dynamodb_config"]))
        monkeypatch.setattr(demo_routes, "_prs", lambda request: list(prs))
        monkeypatch.setattr("compass_api.routes.status._queue_client", queue)
        monkeypatch.setattr(extensions, "_health_checks", {})
        app = FastAPI()
        app.include_router(demo_routes.router)
        yield TestClient(app), table, prs, queue


def _pr(number: int, *, age_ms: int, user: str = "visitor-1", seeded: bool = False) -> Dict[str, Any]:
    return {"repo": "acme/infra", "number": number, "created_at": demo.epoch_ms() - age_ms,
            "origin": {"command": "import", "user_id": user, **({"seeded": True} if seeded else {})}}


def test_the_url_is_public_and_says_what_is_wrong(env):
    client, table, prs, queue = env
    assert "/healthz/visitor" in _PUBLIC_PATHS

    # nothing happened yet: no pull request in a day is the campaign's problem, not a visitor's
    res = client.get("/healthz/visitor")
    assert res.status_code == 503 and res.json()["status"] == "fail" and res.json()["failing"] == ["pull_requests"]
    assert res.json()["checks"]["tour"]["ok"] and res.json()["checks"]["jobs"]["ok"] and res.json()["checks"]["model_budget"]["ok"]
    assert res.json()["checks"]["pull_requests"]["detail"]["newest_visitor_pr_age_minutes"] is None

    # a seeded pull request is not a visitor's; a visitor's from this morning is
    prs.append(_pr(1, age_ms=HOUR, seeded=True))
    assert client.get("/healthz/visitor").status_code == 503
    prs.append(_pr(2, age_ms=5 * HOUR))
    res = client.get("/healthz/visitor")
    assert res.status_code == 200 and res.json()["status"] == "ok" and res.json()["failing"] == []
    assert res.json()["checks"]["pull_requests"]["detail"]["newest_visitor_pr_age_minutes"] == 300

    # yesterday's is too old; the probe's from an hour ago carries it; the probe's from three hours ago does not
    prs[:] = [_pr(3, age_ms=30 * HOUR)]
    assert client.get("/healthz/visitor").status_code == 503
    prs.append(_pr(4, age_ms=HOUR, user="token:probe"))
    assert client.get("/healthz/visitor").status_code == 200
    prs[:] = [_pr(5, age_ms=3 * HOUR, user="token:probe")]
    res = client.get("/healthz/visitor")
    assert res.status_code == 503 and res.json()["checks"]["pull_requests"]["detail"]["newest_probe_pr_age_minutes"] == 180

    # a job queued for twenty minutes means no worker took it
    prs[:] = [_pr(6, age_ms=HOUR)]
    queue.jobs[:] = [{"job_id": "j1", "captain": "iac", "command": "import", "status": "queued", "created_at": demo.epoch_ms() - 20 * 60_000},
                     {"job_id": "j2", "captain": "iac", "command": "import", "status": "queued", "created_at": demo.epoch_ms() - 60_000},
                     {"job_id": "j3", "captain": "engine", "command": "solve", "status": "running", "created_at": demo.epoch_ms() - 50 * 60_000}]
    res = client.get("/healthz/visitor")
    assert res.status_code == 503 and res.json()["failing"] == ["jobs"]
    assert [j["job_id"] for j in res.json()["checks"]["jobs"]["detail"]["stuck"]] == ["j1"] and res.json()["checks"]["jobs"]["detail"]["stuck"][0]["queued_minutes"] == 20
    queue.jobs[:] = []

    # the model budget: this hour's calls at the limit
    from compass_core import model_budget

    for _ in range(3):
        model_budget.claim({**CONFIG, "limits": {"model_calls_per_hour": 3}}, workspace_id=WS)
    res = client.get("/healthz/visitor")
    assert res.status_code == 200, "no limit in the config, no budget to exhaust"
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(demo_routes, "get_workspace_config", lambda request: {**CONFIG, "limits": {"model_calls_per_hour": 3}})
        res = client.get("/healthz/visitor")
        assert res.status_code == 503 and res.json()["failing"] == ["model_budget"] and res.json()["checks"]["model_budget"]["detail"]["exhausted"] == ["hour"]


def test_the_product_adds_checks_and_the_pull_request_check_can_be_turned_off(env, monkeypatch):
    client, table, prs, queue = env
    seen = []
    extensions.register_health_check("workers", lambda config, workspace_id: (seen.append(workspace_id), {"ok": True, "detail": {"engine": 1}})[1])
    extensions.register_health_check("spend", lambda config, workspace_id: {"ok": False, "detail": {"spent_usd": 101.0, "cap_usd": 100.0}})
    extensions.register_health_check("broken", lambda config, workspace_id: 1 / 0)
    prs.append(_pr(1, age_ms=HOUR))
    res = client.get("/healthz/visitor")
    assert res.status_code == 503 and res.json()["failing"] == ["spend", "broken"] and seen == [WS]
    assert res.json()["checks"]["workers"] == {"ok": True, "detail": {"engine": 1}}
    assert res.json()["checks"]["broken"]["detail"]["error"].startswith("ZeroDivisionError")

    monkeypatch.setattr(extensions, "_health_checks", {})
    prs.clear()
    monkeypatch.setattr(demo_routes, "get_workspace_config", lambda request: {**CONFIG, "demo": {"health": {"pr_max_age_hours": 0}}})
    res = client.get("/healthz/visitor")
    assert res.status_code == 200 and "pull_requests" not in res.json()["checks"]


def test_outside_demo_mode_there_is_no_visitor_health(env, monkeypatch):
    client, *_ = env
    monkeypatch.setenv("APP_MODE", "production")
    assert client.get("/healthz/visitor").status_code == 404


def test_the_funnel_leaves_the_probe_out(env):
    client, table, prs, queue = env
    from compass_api import demo_guard

    demo.start_tour(table, PK, "token:probe", story="drift", source={"src": "probe"})
    demo.start_tour(table, PK, "visitor-1", story="drift", source={"src": "gads"})
    prs.append(_pr(1, age_ms=HOUR, user="token:probe"))
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(demo_guard, "is_demo_admin", lambda request, config=None: True)
        funnel = client.get("/api/demo/funnel").json()
    assert funnel["total"]["visitors"] == 1 and funnel["total"]["pr_opened"] == 0 and [r["src"] for r in funnel["by_source"]] == ["gads"]
