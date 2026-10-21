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

def test_migrate_creates_tables_and_runs_each_step_once(monkeypatch, tmp_path):
    import boto3
    from moto import mock_aws

    from compass_core import config as config_module
    from compass_core import migrations

    monkeypatch.setenv("COMPASS_DATA_DIR", str(tmp_path))  # the 0.6.0 step puts the map file here
    monkeypatch.delenv("COMPASS_MAP_STORE", raising=False)
    saved = []
    monkeypatch.setattr(config_module, "save_workspace_config", lambda ws, cfg: saved.append((ws, dict(cfg))))
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
        # the registered steps: 0.6.0 moves the map to SQLite (here: no map table, so it only marks the config)
        assert migrations.run(config, workspace_id="local", client=client)["migrations"] == ["0.6.0-map-sqlite", "0.7.0-access-model", "0.7.0-grants"]
        assert saved[-1][1]["map"]["store"] == "sqlite"
        assert migrations.run(config, workspace_id="local", client=client)["migrations"] == []


def test_a_check_made_by_one_process_reaches_the_other_through_the_volume(install, tmp_path):
    """The API checks when the page asks; the daemon, which applies the policy, sees it on its next tick."""
    volume, publish, _ = install
    publish(_feed(_release("0.5.0")))
    assert updates.check().latest == "0.5.0"               # the daemon's copy
    (tmp_path / "releases.json").write_text(json.dumps(_feed(_release("0.6.0"))))
    record = json.loads((volume / "update.check.json").read_text())
    record.update(document=_feed(_release("0.6.0")), checked_at=time.time() + 5)  # the API's newer check
    (volume / "update.check.json").write_text(json.dumps(record))
    assert updates.check().latest == "0.6.0"


# ── the release notes ───────────────────────────────────────────────────────

def _item(version, *, headline="h", link=None, features=(("Short title", "A paragraph."),), after=(), version_element=True):
    link = link or f"https://beamreach.ai/compass/releases/{version}.html"
    parts = [f"<title>Compass {version} — {headline}</title>", f"<link>{link}</link>",
             f'<guid isPermaLink="true">{link}</guid>', "<pubDate>Fri, 16 Oct 2026 12:00:00 +0000</pubDate>",
             f"<description>Compass {version} in a paragraph.</description>",
             "<content:encoded><![CDATA[<p>HTML for <script>feed readers</script></p>]]></content:encoded>"]
    if version_element:
        parts.append(f"<compass:version>{version}</compass:version>")
    parts.append(f"<compass:headline>{headline}</compass:headline>")
    parts += [f"<compass:feature><compass:title>{t}</compass:title><compass:body>{b}</compass:body></compass:feature>" for t, b in features]
    parts += [f"<compass:after>{a}</compass:after>" for a in after]
    return "<item>" + "".join(parts) + "</item>"


def _notes(*items):
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom" xmlns:content="http://purl.org/rss/1.0/modules/content/"'
            ' xmlns:compass="https://beamreach.ai/compass/releases/ns/1"><channel><title>Compass releases</title>'
            "<link>https://beamreach.ai/compass/releases/</link>" + "".join(items) + "</channel></rss>")


@pytest.fixture
def notes_file(install, tmp_path, monkeypatch):
    path = tmp_path / "feed.xml"
    monkeypatch.setenv("COMPASS_RELEASE_NOTES_URL", f"file://{path}")
    return path


def test_the_notes_feed_is_read_as_plain_text_and_ignores_what_it_should():
    notes = updates.parse_notes(_notes(
        _item("0.4.1", headline="The CLI moves in", features=(("One", "First."), ("Two", "Second.")), after=("Reload the page.",)),
        _item("0.4.2", link="http://beamreach.ai/compass/releases/0.4.2.html", features=()),
        _item("0.4.3", version_element=False),
        _item("not-a-version"),
    ))
    assert set(notes) == {"0.4.1", "0.4.2"}, "an item without a compass version is skipped"
    assert notes["0.4.1"] == {
        "version": "0.4.1", "title": "Compass 0.4.1 — The CLI moves in", "headline": "The CLI moves in",
        "link": "https://beamreach.ai/compass/releases/0.4.1.html", "published_at": "2026-10-16T12:00:00Z",
        "summary": "Compass 0.4.1 in a paragraph.", "features": [{"title": "One", "body": "First."}, {"title": "Two", "body": "Second."}],
        "after": ["Reload the page."],
    }
    assert notes["0.4.2"]["link"] is None and notes["0.4.2"]["features"] == [], "only an https link is kept"
    assert "<p>" not in json.dumps(notes), "content:encoded is never read"

    for bad, why in [("<rss><channel>", "not XML"), ("<feed/>", "an RSS document"),
                     ('<?xml version="1.0"?><!DOCTYPE rss [<!ENTITY a "aaaa">]><rss><channel/></rss>', "DOCTYPE"),
                     (_notes("<!-- " + "x" * updates.NOTES_MAX_BYTES + " -->"), "larger than")]:
        with pytest.raises(ValueError, match=why):
            updates.parse_notes(bad)


def test_the_state_carries_the_notes_between_the_running_and_the_offered_release(install, notes_file, monkeypatch):
    volume, publish, _ = install
    notes_file.write_text(_notes(_item("0.3.0"), _item("0.4.0"), _item("0.4.1", headline="Patch"), _item("0.5.0", headline="Minor"),
                                 _item("0.5.1rc1"), _item("0.6.0")))
    publish(_feed(_release("0.4.0"), _release("0.4.1"), _release("0.5.0"), _release("0.6.0"), channels={"stable": "0.5.0"}))
    state = updates.check()
    assert state.latest == "0.5.0" and [n["version"] for n in state.release_notes] == ["0.5.0", "0.4.1"], \
        "after 0.4.0, up to the offered 0.5.0, newest first, no prerelease on stable"
    assert state.release_notes[0]["headline"] == "Minor" and state.notes_source == f"file://{notes_file}" and state.notes_error is None
    assert (volume / "update.notes.json").exists(), "the notes are kept on the volume"

    monkeypatch.setattr(updates, "running_version", lambda: "0.5.0")
    assert updates.check().release_notes == [], "nothing offered, no notes"


def test_notes_that_cannot_be_read_leave_the_check_alone_and_keep_the_last_copy(install, notes_file):
    _, publish, _ = install
    publish(_feed(_release("0.5.0")))
    assert updates.check().release_notes == [] and "feed.xml" in updates.check().notes_error, "no notes yet: the check still says 0.5.0"
    assert updates.check().available

    notes_file.write_text(_notes(_item("0.5.0")))
    assert [n["version"] for n in updates.check(refresh=True).release_notes] == ["0.5.0"]
    notes_file.write_text('<?xml version="1.0"?><!DOCTYPE rss SYSTEM "x.dtd"><rss/>')
    state = updates.check(refresh=True)
    assert state.available and state.error is None and state.latest == "0.5.0"
    assert [n["version"] for n in state.release_notes] == ["0.5.0"] and "DOCTYPE" in state.notes_error, "the last good copy, with why"


def test_with_the_check_off_the_notes_are_not_fetched_and_off_turns_off_only_the_notes(install, monkeypatch):
    _, publish, _ = install
    publish(_feed(_release("0.5.0")))
    fetched = []
    monkeypatch.setattr(updates, "_get", lambda url, **kw: fetched.append(url) or (_ for _ in ()).throw(OSError("no network in tests")))
    monkeypatch.setenv("COMPASS_RELEASE_NOTES_URL", "https://example.com/feed.xml")
    monkeypatch.setenv("COMPASS_UPDATE_CHECK", "off")
    assert updates.check().release_notes == [] and fetched == []

    monkeypatch.delenv("COMPASS_UPDATE_CHECK")
    monkeypatch.setenv("COMPASS_RELEASE_NOTES_URL", "off")
    monkeypatch.setattr(updates, "_fetch", lambda url, **kw: (200, None, _feed(_release("0.5.0"))))
    state = updates.check(refresh=True)
    assert state.available and state.notes_source is None and state.notes_error is None and fetched == []


def test_the_notes_are_asked_for_with_the_same_user_agent_and_their_etag(install, monkeypatch):
    _, publish, _ = install
    publish(_feed(_release("0.5.0")))
    seen = []

    class Response:
        def __init__(self, status, body):
            self.status, self.body, self.headers = status, body, {"ETag": '"n1"'}

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, limit=-1):
            return self.body

    def urlopen(request, timeout):
        headers = dict(request.header_items())
        seen.append(headers)
        if headers.get("If-none-match") == '"n1"':
            raise updates.urllib.error.HTTPError(request.full_url, 304, "Not Modified", {}, None)
        return Response(200, _notes(_item("0.5.0", headline="Fresh")).encode())

    monkeypatch.setenv("COMPASS_RELEASE_NOTES_URL", "https://example.com/feed.xml")
    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    assert updates.check().release_notes[0]["headline"] == "Fresh"
    assert seen[0]["User-agent"] == "compass-update/0.4.0" and "If-none-match" not in seen[0]
    state = updates.check(refresh=True)
    assert seen[-1]["If-none-match"] == '"n1"' and state.release_notes[0]["headline"] == "Fresh" and state.notes_error is None, "304 keeps the copy"


def test_the_api_returns_the_notes_with_the_state(client, notes_file):
    tc, (volume, publish, _), _ = client
    notes_file.write_text(_notes(_item("0.5.0", headline="Minor", after=("Reload the Captains page.",))))
    publish(_feed(_release("0.5.0")))
    update = tc.get("/api/updates").json()["update"]
    assert update["release_notes"][0]["after"] == ["Reload the Captains page."] and update["notes_error"] is None
