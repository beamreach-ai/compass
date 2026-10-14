import importlib
from types import SimpleNamespace

from botocore.exceptions import ClientError
from fastapi import FastAPI
from fastapi.testclient import TestClient


def _build_client(monkeypatch, config):
    """The open settings routes (AWS, GitHub, bootstrap) with no product sections registered."""
    from compass_api import extensions

    module = importlib.import_module("compass_api.routes.settings")
    # An open install has none of the product's sections or hooks; empty registries, restored afterwards.
    for name in ("_bootstrap_sections", "_status_sections"):
        monkeypatch.setattr(extensions, name, {})
    for name in ("_bootstrap_hooks", "_aws_saved_hooks", "_job_restart_guards"):
        monkeypatch.setattr(extensions, name, [])
    module.invalidate_bootstrap_sections()

    saved = {}

    monkeypatch.setattr(module, "get_workspace_context", lambda _request: SimpleNamespace(workspace_id="user-123", org_id=None, user_id="user-123"))
    monkeypatch.setattr(module, "get_workspace_config", lambda _request: config)
    monkeypatch.setattr(module, "current_infra_scan_summary", lambda _request, **_kwargs: {"scanned": True, "integrations_ready": False})
    monkeypatch.setattr(module, "enqueue_full_infra_scan", lambda *_args, **_kwargs: None)

    def _capture_save(_request, updated):
        saved["config"] = updated

    monkeypatch.setattr(module, "save_config", _capture_save)

    app = FastAPI()
    app.include_router(module.router)
    return TestClient(app), saved


def test_settings_bootstrap_redacts_secrets(monkeypatch):
    config = {
        "aws": {
            "credentials": {
                "access_key_id": "AKIAIOSFODNN7EXAMPLE",
                "secret_access_key": "secret-value",
            },
            "account_id": "123456789012",
            "region": "eu-central-1",
        },
        "github": {
            "installation_id": 42,
            "account_login": "beamreach-ai",
            "repository_selection": "selected",
            "repositories_count": 3,
        },
    }

    client, _saved = _build_client(monkeypatch, config)
    monkeypatch.setenv("GITHUB_APP_ID", "3209488")
    monkeypatch.setenv("GITHUB_APP_CLIENT_ID", "Iv23liILpVpvVkNhmW15")
    monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY", "-----BEGIN PRIVATE KEY-----\\nfake\\n-----END PRIVATE KEY-----")
    monkeypatch.setenv("GITHUB_APP_SLUG", "beamreach-compass")

    response = client.get("/api/settings/bootstrap")
    assert response.status_code == 200

    body = response.json()
    assert body["workspace"]["workspace_id"] == "user-123"
    assert body["aws"]["configured"] is True
    assert body["aws"]["access_key_id_hint"] == "...MPLE"
    assert "secret_access_key" not in body["aws"]
    assert body["github"]["connected"] is True
    assert body["github"]["login"] == "beamreach-ai"
    assert body["github"]["installation_id"] == 42
    assert body["github"]["repository_selection"] == "selected"
    assert body["github"]["repositories_count"] == 3
    assert body["github"]["app_configured"] is True
    assert body["github"]["public_link"] == "https://github.com/apps/beamreach-compass"
    # triage and billing are the product's sections; the open edition reports None
    assert body["triage"] is None and body["billing"] is None
    assert body["infra_scan"]["scanned"] is True
    assert "token" not in body["github"]


def test_update_aws_settings_persists_assume_role(monkeypatch):
    config = {}
    client, saved = _build_client(monkeypatch, config)

    module = importlib.import_module("compass_api.routes.settings")
    monkeypatch.setattr(
        module,
        "_validate_aws_access",
        lambda payload: {
            "account_id": "123456789012",
            "region": "eu-central-1",
            "caller_arn": "arn:aws:sts::123456789012:assumed-role/TestRole/session",
        },
    )

    response = client.post(
        "/api/settings/aws",
        json={
            "mode": "assume_role",
            "region": "eu-central-1",
            "account_name": "Production",
            "assume_role": {
                "account_id": "123456789012",
                "role_name": "BeamreachCompassRole",
                "external_id": "external-123",
            },
        },
    )
    assert response.status_code == 200

    stored = saved["config"]
    assert stored["provider"] == "aws"
    assert stored["aws_region"] == "eu-central-1"
    assert stored["aws"]["account_id"] == "123456789012"
    assert stored["aws"]["account_name"] == "Production"
    assert stored["aws"]["assume_role"]["role_name"] == "BeamreachCompassRole"
    assert stored["aws"]["assume_role"]["external_id"] == "external-123"
    assert "credentials" not in stored["aws"]


def test_test_aws_settings_returns_400_for_assume_role_access_denied(monkeypatch):
    config = {}
    client, _saved = _build_client(monkeypatch, config)

    module = importlib.import_module("compass_api.routes.settings")

    def _raise_client_error(*_args, **_kwargs):
        raise ClientError(
            {
                "Error": {
                    "Code": "AccessDenied",
                    "Message": (
                        "User: arn:aws:iam::123456789012:user/deployer is not authorized to perform: "
                        "sts:AssumeRole on resource: arn:aws:iam::123456789012:role/BeamreachCompassRole"
                    ),
                }
            },
            "AssumeRole",
        )

    monkeypatch.setattr(module, "build_target_session", _raise_client_error)

    response = client.post(
        "/api/settings/aws/test",
        json={
            "mode": "assume_role",
            "assume_role": {
                "account_id": "123456789012",
                "role_name": "BeamreachCompassRole",
            },
        },
    )

    assert response.status_code == 400
    assert response.json()["detail"].startswith(
        "Failed to assume AWS role arn:aws:iam::123456789012:role/BeamreachCompassRole: AccessDenied:"
    )
    assert "sts:AssumeRole" in response.json()["detail"]
    assert "trust policy permits this principal" in response.json()["detail"]


def test_github_setup_persists_installation(monkeypatch):
    config = {"github": {"token": "legacy-token"}}
    client, saved = _build_client(monkeypatch, config)

    module = importlib.import_module("compass_api.routes.settings")
    monkeypatch.setattr(
        module,
        "load_github_app_env",
        lambda: type("Env", (), {"slug": "beamreach-compass", "public_link": "https://github.com/apps/beamreach-compass"})(),
    )
    monkeypatch.setattr(
        module,
        "get_installation_metadata",
        lambda installation_id, app_env=None: {
            "id": installation_id,
            "target_id": 123,
            "repository_selection": "selected",
            "account": {"login": "beamreach-ai", "type": "Organization", "id": 456},
        },
    )
    monkeypatch.setattr(
        module,
        "list_installation_repositories",
        lambda installation_id, app_env=None: {"total_count": 2},
    )

    client.cookies.set(
        "compass_github_install",
        '{"nonce":"abc","workspace_id":"user-123","user_id":"user-123","issued_at":123}',
        path="/api/integrations/github",
    )
    response = client.get("/api/integrations/github/setup?installation_id=99&setup_action=install", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "/workspace?tab=settings&github=connected"

    github_cfg = saved["config"]["github"]
    assert github_cfg["provider"] == "github_app"
    assert github_cfg["installation_id"] == 99
    assert github_cfg["account_login"] == "beamreach-ai"
    assert github_cfg["repository_selection"] == "selected"
    assert github_cfg["repositories_count"] == 2
    assert "token" not in github_cfg
