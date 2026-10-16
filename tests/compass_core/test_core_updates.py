"""Knowing a release is out and asking the updater for it (docs/open_updates_spec.md):
the feed, the state, the refusals, rollback, the policy and its window, the files
the updater reads, the API, the CLI verbs, migrate, and the extensions rebuild."""

from __future__ import annotations

import json
import time
from datetime import datetime

import pytest

from compass_core import captains, extensions, updates
from compass_core.captains import CaptainManifest


def _release(version, *, min_from=None, summary="s", notes=()):
    return {"version": version, "published_at": "2026-10-16T15:40:00Z", "notes_url": f"https://example.com/v{version}",
            "summary": summary, "images": {"compass": f"sha256:{version}", "compass-dashboard": f"sha256:d{version}"},
            "min_from": min_from, "requires_restart": ["api", "compass", "dashboard"], "notes": list(notes)}


def _feed(*releases, channels=None):
    return {"schema": 1, "channels": channels or {}, "releases": list(releases)}


@pytest.fixture
def install(tmp_path, monkeypatch):
    """An install on 0.4.0 with an extensions volume and a feed on disk; the check on."""
    volume = tmp_path / "extensions"
    feed = tmp_path / "releases.json"
    monkeypatch.setenv("COMPASS_EXTENSIONS_DIR", str(volume))
    monkeypatch.setenv("COMPASS_UPDATE_URL", str(feed))
    for name in ("COMPASS_UPDATE_CHECK", "COMPASS_AUTO_UPDATE", "COMPASS_UPDATE_WINDOW", "COMPASS_UPDATE_CHANNEL", "COMPASS_VERSION"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(updates, "running_version", lambda: "0.4.0")
    captains.reset_for_tests()

    def publish(document):
        feed.write_text(json.dumps(document))
        updates.reset_for_tests()
        (volume / "update.check.json").unlink(missing_ok=True)

    def updater(alive=True):
        volume.mkdir(parents=True, exist_ok=True)
        extensions.write_json(volume / "updater.json", {"at": time.time() if alive else time.time() - 3600, "version": "0.4.0"})

    yield volume, publish, updater
    captains.reset_for_tests()


def test_a_newer_release_is_available_with_its_notes_and_the_pin_is_compared(install, monkeypatch):
    volume, publish, _ = install
    publish(_feed(_release("0.5.0", min_from="0.4.0", notes=["Reload the Captains page."]), _release("0.4.0"),
                  _release("0.6.0rc1"), channels={"stable": "0.5.0", "prerelease": "0.6.0rc1"}))
    monkeypatch.setenv("COMPASS_VERSION", "0.3.0")
    state = updates.check()
    assert (state.running, state.latest, state.available, state.direct) == ("0.4.0", "0.5.0", True, True)
    assert state.release["notes"] == ["Reload the Captains page."] and state.apply == "manual"
    assert state.pinned == "0.3.0" and state.pinned_differs, "compose asks for 0.3.0 but 0.4.0 runs"
    assert [r["version"] for r in state.releases] == ["0.5.0"], "no prerelease on the stable channel"
    assert (volume / "update.check.json").exists(), "the fetch is kept on the volume"

    monkeypatch.setenv("COMPASS_UPDATE_CHANNEL", "prerelease")
    assert updates.check().latest == "0.6.0rc1"
    monkeypatch.setenv("COMPASS_UPDATE_CHECK", "off")
    off = updates.check()
    assert off.apply == "off" and off.latest is None and not off.available


def test_the_feed_is_fetched_once_per_period_and_a_failure_keeps_the_last_copy(install, tmp_path, monkeypatch):
    _, publish, _ = install
    publish(_feed(_release("0.5.0")))
    assert updates.check().latest == "0.5.0"
    (tmp_path / "releases.json").write_text(json.dumps(_feed(_release("0.6.0"))))
    assert updates.check().latest == "0.5.0", "cached for six hours"
    assert updates.check(refresh=True).latest == "0.6.0"
    (tmp_path / "releases.json").unlink()
    failed = updates.check(refresh=True)
    assert failed.latest == "0.6.0" and "releases.json" in failed.error, "the last good copy, with why the fetch failed"


def test_the_check_sends_only_the_version_in_the_user_agent(install, monkeypatch):
    seen = {}

    class Response:
        status = 200
        headers = {"ETag": '"abc"'}

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps(_feed(_release("0.5.0"))).encode()

    def urlopen(request, timeout):
        seen.update(url=request.full_url, headers=dict(request.header_items()))
        return Response()

    monkeypatch.setenv("COMPASS_UPDATE_URL", "https://example.com/releases.json")
    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    assert updates.check().latest == "0.5.0"
    assert seen["headers"]["User-agent"] == "compass-update/0.4.0" and set(seen["headers"]) == {"User-agent", "Accept"}
    updates.check(refresh=True)
    assert seen["headers"]["If-none-match"] == '"abc"'


def test_apply_writes_the_request_and_refuses_what_it_should(install, monkeypatch):
    volume, publish, updater = install
    publish(_feed(_release("0.6.0", min_from="0.5.0"), _release("0.5.0", min_from="0.4.0"), _release("0.3.0")))

    with pytest.raises(updates.UpdatesError) as info:
        updates.request_apply()
    assert info.value.code == "no_updater" and "COMPASS_VERSION=0.6.0" in info.value.extra["commands"][0]

    updater()
    state = updates.check()
    assert state.latest == "0.6.0" and not state.direct and state.via == "0.5.0", "step through 0.5.0"
    with pytest.raises(updates.UpdatesError) as info:
        updates.request_apply()
    assert info.value.code == "not_direct" and info.value.extra["via"] == "0.5.0"
    with pytest.raises(updates.UpdatesError) as info:
        updates.request_apply("0.3.0")
    assert info.value.code == "downgrade"
    with pytest.raises(updates.UpdatesError) as info:
        updates.request_apply("9.9.9")
    assert info.value.code == "unknown_release"

    captains.register(CaptainManifest(name="old", label="Old", summary="s", tier="open", feature="old", requires_core=">=0.4,<0.5"))
    with pytest.raises(updates.UpdatesError) as info:
        updates.request_apply("0.5.0")
    assert info.value.code == "captains_incompatible" and info.value.extra["captains"] == {"old": ">=0.4,<0.5"}

    record = updates.request_apply("0.5.0", force=True, by="ilia")
    on_disk = json.loads((volume / "update.json").read_text())
    assert on_disk == record and record["target"] == "0.5.0" and record["digests"]["compass"] == "sha256:0.5.0"
    assert record["mode"] == "manual" and record["force"] is True and record["from"] == "0.4.0"
    with pytest.raises(updates.UpdatesError) as info:
        updates.request_apply("0.5.0", force=True)
    assert info.value.code == "request_pending"


def test_a_dead_updater_is_no_updater(install):
    _, publish, updater = install
    publish(_feed(_release("0.5.0")))
    updater(alive=False)
    assert updates.check().apply == "manual"


def test_rollback_is_offered_for_seven_days_and_asks_for_the_previous_version(install):
    volume, publish, updater = install
    publish(_feed(_release("0.4.0"), _release("0.3.0")))
    with pytest.raises(updates.UpdatesError) as info:
        updates.request_rollback()
    assert info.value.code == "nothing_to_roll_back"
    extensions.write_json(volume / "rollback.json", {"to": "0.3.0", "until": time.time() - 1})
    assert updates.rollback_offer() is None, "expired"
    extensions.write_json(volume / "rollback.json", {"to": "0.3.0", "until": time.time() + 86400})
    updater()
    record = updates.request_rollback(by="ilia")
    assert record["target"] == "0.3.0" and record["rollback"] is True and record["force"] is True


def test_policy_steps_and_the_window():
    assert updates.allowed_step("patch", "0.4.0", "0.4.1") and not updates.allowed_step("patch", "0.4.0", "0.5.0")
    assert updates.allowed_step("minor", "0.4.0", "0.5.0") and not updates.allowed_step("minor", "0.9.0", "1.0.0")
    assert updates.allowed_step("minor", "1.2.0", "1.3.4") and not updates.allowed_step("minor", "0.4.0", "0.5.0rc1")
    assert not updates.allowed_step("notify", "0.4.0", "0.4.1")


def test_the_window_bounds_automatic_applies_even_across_midnight(monkeypatch):
    monkeypatch.setenv("COMPASS_UPDATE_WINDOW", "23:00-02:00")
    assert updates.in_window(datetime(2026, 10, 16, 23, 30)) and updates.in_window(datetime(2026, 10, 17, 1, 59))
    assert not updates.in_window(datetime(2026, 10, 16, 12, 0))
    monkeypatch.setenv("COMPASS_UPDATE_WINDOW", "nonsense")
    assert updates.in_window(datetime(2026, 10, 16, 12, 0)), "a window that does not parse is no window"


def test_the_workspace_policy_wins_over_the_environment(monkeypatch):
    monkeypatch.setenv("COMPASS_AUTO_UPDATE", "patch")
    assert updates.policy({})["auto"] == "patch"
    assert updates.policy({"updates": {"auto": "notify"}}) == {"auto": "notify", "window": None, "source": "config"}
    assert not updates.check_enabled({"updates": {"auto": "off"}})


def test_the_policy_requests_an_automatic_update_once_and_not_after_it_failed(install, monkeypatch):
    volume, publish, updater = install
    publish(_feed(_release("0.4.1", min_from="0.4.0"), _release("0.5.0", min_from="0.4.0"), channels={"stable": "0.4.1"}))
    updater()
    assert updates.auto_request({})[0] is None, "notify by default"
    monkeypatch.setenv("COMPASS_AUTO_UPDATE", "patch")
    monkeypatch.setenv("COMPASS_UPDATE_WINDOW", "03:00-05:00")
    assert "outside the window" in updates.auto_request({}, now=datetime(2026, 10, 16, 12, 0))[1]
    requested, _ = updates.auto_request({}, now=datetime(2026, 10, 16, 3, 30))
    assert requested["target"] == "0.4.1" and requested["mode"] == "auto" and requested["by"] == "policy"
    assert updates.auto_request({}, now=datetime(2026, 10, 16, 3, 31))[0] is None, "one request at a time"

    (volume / "update.json").unlink()
    extensions.write_json(volume / "update.result.json", {"status": "failed", "from": "0.4.0", "to": "0.4.1", "mode": "auto", "at": time.time()})
    assert "not retried" in updates.auto_request({}, now=datetime(2026, 10, 16, 3, 32))[1]


def test_status_json_counts_active_jobs(install, monkeypatch):
    volume, _, _ = install

    class Store:
        def list_jobs(self, limit, workspace_id):
            return [{"status": "running"}, {"status": "queued"}, {"status": "completed"}]

    monkeypatch.setattr("compass_core.jobs.store_for", lambda config: Store())
    updates.write_status(updates.count_active_jobs({}, "local"), by="api")
    assert json.loads((volume / "status.json").read_text())["active_jobs"] == 2


def test_the_policy_is_mirrored_for_the_updater(install, monkeypatch):
    volume, _, _ = install
    monkeypatch.setenv("COMPASS_UPDATE_WINDOW", "03:00-05:00")
    updates.mirror_policy({"updates": {"auto": "minor"}})
    mirrored = json.loads((volume / "update.policy.json").read_text())
    assert (mirrored["auto"], mirrored["window"], mirrored["channel"]) == ("minor", "03:00-05:00", "stable")


# ── the API ─────────────────────────────────────────────────────────────────

@pytest.fixture
def client(install, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from compass_api.routes import updates as routes

    config = {}
    saved = []
    for name in ("READ_ONLY", "DISABLED_FEATURES"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(routes, "get_workspace_config", lambda request: config)
    monkeypatch.setattr(routes, "save_config", lambda request, cfg: saved.append(json.loads(json.dumps(cfg))))
    monkeypatch.setattr(routes, "get_actor_ids", lambda request: {"user_id": "ilia", "workspace_id": "local", "org_id": None})
    app = FastAPI()
    app.include_router(routes.router)
    return TestClient(app), install, saved


def test_the_page_updates_with_the_updater_and_gets_the_commands_without(client, monkeypatch):
    tc, (volume, publish, updater), saved = client
    publish(_feed(_release("0.5.0", min_from="0.4.0")))
    body = tc.get("/api/updates").json()
    assert body["update"]["available"] and body["update"]["apply"] == "manual" and body["policy"]["auto"] == "notify"
    refused = tc.post("/api/updates/apply", json={})
    assert refused.status_code == 409 and refused.json()["code"] == "no_updater" and refused.json()["commands"]

    updater()
    done = tc.post("/api/updates/apply", json={"target": "0.5.0"})
    assert done.status_code == 202 and done.json()["status"] == "requested" and done.json()["request"]["by"] == "ilia"
    assert tc.get("/api/updates").json()["request"]["target"] == "0.5.0"

    monkeypatch.setenv("READ_ONLY", "1")
    assert tc.post("/api/updates/rollback").status_code == 403


def test_the_policy_is_saved_in_the_workspace_config(client):
    tc, (volume, _, _), saved = client
    assert tc.put("/api/updates/policy", json={"auto": "sometimes"}).status_code == 422
    body = tc.put("/api/updates/policy", json={"auto": "patch"}).json()
    assert body["policy"]["auto"] == "patch" and saved[-1]["updates"] == {"auto": "patch"}
    assert json.loads((volume / "update.policy.json").read_text())["auto"] == "patch"


def test_update_writes_need_the_admin_scope():
    from compass_api.auth import scopes

    assert scopes.required_scope("POST", "/api/updates/apply") == "admin"
    assert scopes.required_scope("GET", "/api/updates") == "read"


# ── the CLI ─────────────────────────────────────────────────────────────────

def test_compass_update_check_and_apply_from_the_shell(install, capsys, monkeypatch):
    from compass_core import cli

    volume, publish, updater = install
    monkeypatch.setattr("compass_core.commands.resolve", lambda payload: (_ for _ in ()).throw(LookupError("no workspace")))
    publish(_feed(_release("0.5.0", min_from="0.4.0")))
    assert cli.main(["update", "check"]) == 0
    assert json.loads(capsys.readouterr().out)["update"]["latest"] == "0.5.0"
    assert cli.main(["update", "apply"]) == 2
    err = capsys.readouterr().err
    assert "No updater is running" in err and "docker compose pull && docker compose up -d" in err
    updater()
    assert cli.main(["update", "apply", "version=0.5.0"]) == 0
    assert json.loads(capsys.readouterr().out)["requested"]["target"] == "0.5.0"
    assert cli.main(["core", "update", "verb=sideways"]) == 2


# ── the API reports the code's version ──────────────────────────────────────

def test_the_reported_version_is_the_code_not_the_environment(monkeypatch):
    from compass_api.version import compass_version
    from compass_core import __version__

    monkeypatch.setenv("COMPASS_VERSION", "0.1.0")
    assert compass_version() == __version__ and updates.pinned_version() == "0.1.0"


# ── extensions on a new core ────────────────────────────────────────────────

def test_extensions_are_rebuilt_when_a_new_core_runs(tmp_path, monkeypatch):
    import sys

    from tests.compass_core.test_core_extensions import FAKE_PIP

    path = tmp_path / "extensions"
    monkeypatch.setenv("COMPASS_EXTENSIONS_DIR", str(path))
    monkeypatch.setenv("COMPASS_EXTENSIONS", "hello==0.1.0")
    pip_script = tmp_path / "fake_pip.py"
    pip_script.write_text(FAKE_PIP)
    pip = [sys.executable, str(pip_script)]
    assert extensions.sync(pip=pip)["changed"]
    assert extensions.sync(pip=pip)["changed"] is False
    record = json.loads((path / "installed.json").read_text())
    record["core"] = "0.0.1"  # built by an older Compass
    (path / "installed.json").write_text(json.dumps(record))
    assert extensions.sync(pip=pip)["changed"], "a new core rebuilds site/"
    from compass_core import __version__

    assert extensions.installed_core() == __version__


# ── migrate ─────────────────────────────────────────────────────────────────

def test_migrate_creates_tables_and_runs_each_step_once(monkeypatch):
    import boto3
    from moto import mock_aws

    from compass_core import migrations

    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    config = {"dynamodb_config": {"workspace_id": "local", "items": "compass_items_local", "jobs": "compass_jobs_local"}}
    ran = []
    steps = [("0.5.0", lambda cfg: ran.append("0.5.0")), ("0.6.0", lambda cfg: ran.append("0.6.0"))]
    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-east-1")
        first = migrations.run(config, workspace_id="local", client=client, steps=steps[:1])
        assert set(first["created"]) == {"compass_items_local", "compass_jobs_local"} and first["migrations"] == ["0.5.0"]
        second = migrations.run(config, workspace_id="local", client=client, steps=steps)
        assert second["created"] == [] and second["migrations"] == ["0.6.0"] and ran == ["0.5.0", "0.6.0"]
        assert migrations.applied(config, workspace_id="local") == ["0.5.0", "0.6.0"]
        assert migrations.run(config, workspace_id="local", client=client)["migrations"] == [], "none registered today"
