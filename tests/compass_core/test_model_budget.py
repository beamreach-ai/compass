"""The model budget (compass_core.model_budget): per hour and per day, atomic, refused before the call."""

from __future__ import annotations

from datetime import datetime, timezone

import boto3
import pytest
from moto import mock_aws

from compass_core import llm, model_budget

NOW = datetime(2026, 10, 22, 9, 30, tzinfo=timezone.utc)


@pytest.fixture
def table(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    with mock_aws():
        yield boto3.resource("dynamodb").create_table(
            TableName="items", KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}, {"AttributeName": "sk", "AttributeType": "S"}], BillingMode="PAY_PER_REQUEST")


def _config(**limits):
    return {"workspace_id": "ws", "dynamodb_config": {"items": "items"}, "limits": limits}


def test_no_limit_outside_demo_mode_and_the_demo_defaults_inside(monkeypatch):
    monkeypatch.delenv("APP_MODE", raising=False)
    assert model_budget.limits({}) == {"hour": None, "day": None}
    monkeypatch.setenv("APP_MODE", "demo")
    assert model_budget.limits({}) == model_budget.DEMO_DEFAULTS
    assert model_budget.limits(_config(model_calls_per_hour=5)) == {"hour": 5, "day": model_budget.DEMO_DEFAULTS["day"]}


def test_the_hour_refuses_past_its_limit_and_the_next_hour_starts_again(table):
    config = _config(model_calls_per_hour=3)
    for _ in range(3):
        model_budget.claim(config, table=table, now=NOW)
    with pytest.raises(model_budget.ModelBudgetExceeded) as refused:
        model_budget.claim(config, table=table, now=NOW)
    assert refused.value.window == "hour" and refused.value.limit == 3 and refused.value.used == 3
    model_budget.claim(config, table=table, now=NOW.replace(hour=10))
    assert model_budget.usage(config, table=table, now=NOW)["hour"] == {"used": 3, "limit": 3}


def test_a_day_refused_gives_the_hour_back(table):
    config = _config(model_calls_per_hour=10, model_calls_per_day=2)
    model_budget.claim(config, table=table, now=NOW)
    model_budget.claim(config, table=table, now=NOW)
    with pytest.raises(model_budget.ModelBudgetExceeded) as refused:
        model_budget.claim(config, table=table, now=NOW)
    assert refused.value.window == "day"
    assert model_budget.usage(config, table=table, now=NOW) == {"hour": {"used": 2, "limit": 10}, "day": {"used": 2, "limit": 2}}


def test_zero_means_no_calls_at_all(table):
    with pytest.raises(model_budget.ModelBudgetExceeded):
        model_budget.claim(_config(model_calls_per_hour=0), table=table, now=NOW)


def test_demo_mode_without_a_table_refuses(monkeypatch):
    monkeypatch.setenv("APP_MODE", "demo")
    with pytest.raises(model_budget.ModelBudgetExceeded):
        model_budget.claim({"workspace_id": "ws"}, table=None)


class _Model:
    def __init__(self):
        self.calls = 0

    def call(self, messages, max_tokens=100, temperature=0, timeout=10, system_prompt=None):
        self.calls += 1
        return "ok"


class _CountsItself(_Model):
    budgeted = True


def test_every_call_through_the_seam_is_claimed_first(table, monkeypatch):
    model = _Model()
    monkeypatch.setattr(model_budget, "_table", lambda config: table)
    llm.set_model_factory(lambda config, target: model)
    try:
        wrapped = llm.model_for(_config(model_calls_per_hour=2))
        assert wrapped.call([]) == "ok" and wrapped.call([]) == "ok"
        with pytest.raises(model_budget.ModelBudgetExceeded):
            wrapped.call([])
        assert model.calls == 2, "the refused call never reached the model"

        itself = _CountsItself()
        llm.set_model_factory(lambda config, target: itself)
        assert llm.model_for(_config(model_calls_per_hour=0)) is itself, "a model that counts its own calls is not counted twice"
    finally:
        llm.set_model_factory(None)
