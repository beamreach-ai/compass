import importlib
from types import SimpleNamespace


def test_mapper_org_id_does_not_fallback_to_workspace_config(monkeypatch):
    module = importlib.import_module("compass_api.routes._infra_scan")
    module = importlib.reload(module)

    monkeypatch.setattr(module, "get_actor_ids", lambda _request: {"workspace_id": "beamreach-demo", "org_id": None, "user_id": "user-123"})
    monkeypatch.setattr(module, "get_workspace_context", lambda _request: SimpleNamespace(workspace_id="beamreach-demo", org_id=None, user_id="user-123"))

    assert module._mapper_org_id(object(), {"org_id": "beamreach", "org": "beamreach"}) is None


def test_mapper_org_id_keeps_explicit_override(monkeypatch):
    module = importlib.import_module("compass_api.routes._infra_scan")
    module = importlib.reload(module)

    monkeypatch.setattr(module, "get_actor_ids", lambda _request: {"workspace_id": "beamreach-demo", "org_id": None, "user_id": "user-123"})
    monkeypatch.setattr(module, "get_workspace_context", lambda _request: SimpleNamespace(workspace_id="beamreach-demo", org_id=None, user_id="user-123"))

    assert module._mapper_org_id(object(), {"org_id": "beamreach"}, org_id_override="explicit-org") == "explicit-org"
