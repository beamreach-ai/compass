import importlib

from fastapi import FastAPI
from fastapi.testclient import TestClient


class _FakeStore:
    def fetch_graph(self, org_id):
        return {
            "nodes": [],
            "edges": [],
        }


def _build_client(monkeypatch):
    module = importlib.import_module("compass_api.routes.librarian")
    module = importlib.reload(module)

    monkeypatch.setattr(
        module,
        "resolve_infra_map_context",
        lambda _request: {
            "store": _FakeStore(),
            "default_org": "org-1",
            "workspace_id": "workspace-1",
        },
    )

    class _StubLibrarian:
        def __init__(self, store):
            self.store = store

        def get_terraform_location(self, org_id, resource_id, *, max_candidates=3):
            return {
                "status": "verified",
                "resource_id": resource_id,
                "answer": {
                    "repo": "beamreach/beamreach-coworker",
                    "file": "infra/modules/on-premise/main.tf",
                    "tf_root_module": "infra/environments/demo",
                    "tf_root_module_id": "tfroot://github/beamreach/beamreach-coworker/infra/environments/demo",
                    "tf_address": "module.on-premise.aws_ecs_service.engine",
                    "tf_address_id": "tfaddr://demo#module.on-premise.aws_ecs_service.engine",
                    "module_path": "module.on-premise",
                    "tf_state_file_id": "tfstate://s3/beamreach-tf-states/coworkers/demo/terraform.tfstate",
                    "state_backend": "s3://beamreach-tf-states/coworkers/demo/terraform.tfstate",
                },
                "candidates": [],
                "evidence": [],
                "path": [],
                "freshness": {"freshness_class": "fresh"},
                "ambiguity_notes": [],
                "missing_data_notes": [],
            }

    monkeypatch.setattr(module, "InfraMapLibrarian", _StubLibrarian)

    app = FastAPI()
    app.include_router(module.router)
    return TestClient(app)


def test_librarian_terraform_location_endpoint(monkeypatch):
    client = _build_client(monkeypatch)

    response = client.get(
        "/api/librarian/terraform-location",
        params={"resource_id": "arn:aws:ecs:eu-central-1:123456789012:service/demo/engine"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "verified"
    assert body["workspace_id"] == "workspace-1"
    assert body["org_id"] == "org-1"
    assert body["answer"]["repo"] == "beamreach/beamreach-coworker"
    assert body["answer"]["file"] == "infra/modules/on-premise/main.tf"
