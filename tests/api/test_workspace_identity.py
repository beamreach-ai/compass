from types import SimpleNamespace

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from compass_api.middleware.workspace import WorkspaceContextMiddleware


def test_authenticated_user_workspace_overrides_header(monkeypatch):
    captured = {}

    def fake_load_workspace_context(workspace_id, *, org_id=None, user_id=None, force_refresh=False):
        captured["workspace_id"] = workspace_id
        captured["org_id"] = org_id
        captured["user_id"] = user_id
        captured["force_refresh"] = force_refresh
        return SimpleNamespace(workspace_id=workspace_id, org_id=org_id, user_id=user_id)

    # A signed-in user's own id is their workspace: the rule is installed through the
    # open seam (compass_api.workspace.install(user_workspace=...)); the open default is "never".
    monkeypatch.setattr("compass_api.workspace._user_workspace", lambda user_id: bool(user_id))
    monkeypatch.setattr("compass_api.workspace._loader", fake_load_workspace_context)

    app = FastAPI()
    app.add_middleware(WorkspaceContextMiddleware, optional=False)

    @app.get("/probe")
    def probe(request: Request):
        ctx = request.state.workspace_ctx
        return {"workspace_id": ctx.workspace_id, "user_id": ctx.user_id}

    client = TestClient(app)
    response = client.get(
        "/probe",
        headers={
            "X-User-Id": "user-123",
            "X-Workspace-Id": "other-workspace",
            "X-Org-Id": "ignored-org",
        },
    )

    assert response.status_code == 200
    assert response.json() == {"workspace_id": "user-123", "user_id": "user-123"}
    assert captured == {
        "workspace_id": "user-123",
        "org_id": None,
        "user_id": "user-123",
        "force_refresh": False,
    }
