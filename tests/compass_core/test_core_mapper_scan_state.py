"""The open mapper records the scan in the workspace's infra-scan state (what un-greys the dashboard)."""

import json

import pytest

from compass_core import config as core_config


@pytest.fixture
def workspace_file(tmp_path, monkeypatch):
    path = tmp_path / "local.json"
    path.write_text(json.dumps({"workspace_id": "local", "dynamodb_config": {"map": "m"}}))
    monkeypatch.setenv("COMPASS_CONFIG", str(path))
    monkeypatch.setattr(core_config, "_loader", None)
    monkeypatch.setattr(core_config, "_saver", None)
    from compass_api import workspace as api_workspace

    monkeypatch.setattr(api_workspace, "_loader", api_workspace._open_loader)
    monkeypatch.setattr(api_workspace, "_saver", api_workspace._open_saver)
    return path


def test_a_completed_scan_is_recorded(workspace_file, monkeypatch):
    from captains.mapper import commands

    monkeypatch.setattr("captains.mapper.captain.MapperCaptain.run", lambda self: None)
    commands.run({"workspace_id": "local"})
    state = json.loads(workspace_file.read_text())["infra_scan"]
    assert state["scanned"] is True and state["last_completed_at"]


def test_a_failed_scan_is_recorded_and_raised(workspace_file, monkeypatch):
    from captains.mapper import commands

    def boom(self):
        raise RuntimeError("no credentials")

    monkeypatch.setattr("captains.mapper.captain.MapperCaptain.run", boom)
    with pytest.raises(RuntimeError):
        commands.run({"workspace_id": "local"})
    state = json.loads(workspace_file.read_text())["infra_scan"]
    assert state.get("scanned") is not True and "no credentials" in str(state.get("last_error"))
