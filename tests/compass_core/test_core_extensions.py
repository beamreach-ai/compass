"""Extensions installed from the Captains page: the record, sync (install,
rebuild, failure), restart, and the API routes (docs/captain_extensions_spec.md §10, M2–M3)."""

from __future__ import annotations

import json
import sys
import textwrap

import pytest

from compass_core import captains, catalog, extensions

FAKE_PIP = textwrap.dedent('''
    import sys, pathlib
    args, target, reqs, skip = sys.argv[1:], None, [], False
    for i, a in enumerate(args):
        if skip:
            skip = False
            continue
        if a in ("--target", "--find-links"):
            if a == "--target":
                target = pathlib.Path(args[i + 1])
            skip = True
        elif not a.startswith("--") and a != "install":
            reqs.append(a)
    for r in reqs:
        if "boom" in r:
            print("ERROR: no matching distribution for " + r, file=sys.stderr)
            sys.exit(1)
        (target / (r.split("@")[0].split("==")[0] + ".installed")).write_text(r)
''')


@pytest.fixture
def ext(tmp_path, monkeypatch):
    path = tmp_path / "extensions"
    monkeypatch.setenv("COMPASS_EXTENSIONS_DIR", str(path))
    monkeypatch.delenv("COMPASS_EXTENSIONS", raising=False)
    monkeypatch.delenv("COMPASS_CATALOG_INSTALL", raising=False)
    monkeypatch.setenv("COMPASS_SELF_RESTART", "1")
    pip = tmp_path / "fake_pip.py"
    pip.write_text(FAKE_PIP)
    return path, [sys.executable, str(pip)]


def test_unmanaged_without_a_directory(monkeypatch):
    monkeypatch.delenv("COMPASS_EXTENSIONS_DIR", raising=False)
    assert extensions.directory() is None and not extensions.managed()
    assert extensions.sync()["changed"] is False
    with pytest.raises(extensions.ExtensionsError) as info:
        extensions.request_install("x", "x==1")
    assert info.value.code == "extensions_unmanaged"


def test_requirements_split_on_spaces_and_commas(monkeypatch):
    monkeypatch.setenv("COMPASS_EXTENSIONS", "a==1, b@git+https://x/y.git@v1\nc==2")
    assert extensions.from_env() == ["a==1", "b@git+https://x/y.git@v1", "c==2"]


def test_install_sync_upgrade_uninstall(ext, monkeypatch):
    path, pip = ext
    monkeypatch.setenv("COMPASS_EXTENSIONS", "fromenv==1")
    extensions.request_install("hello", "hello==0.1.0", version="0.1.0", by="ilia")
    assert extensions.wanted() == ["fromenv==1", "hello==0.1.0"] and extensions.pending()

    first = extensions.sync(pip=pip)
    assert first["changed"] and sorted((path / "site").iterdir()) == [path / "site" / "fromenv.installed", path / "site" / "hello.installed"]
    assert not extensions.pending()
    assert extensions.sync(pip=pip)["changed"] is False, "nothing to do the second time"

    extensions.request_install("hello", "hello==0.2.0", version="0.2.0")  # an upgrade replaces the pin
    assert [r["pip"] for r in extensions.requested()] == ["hello==0.2.0"]
    extensions.sync(pip=pip)
    assert (path / "site" / "hello.installed").read_text() == "hello==0.2.0"

    extensions.request_uninstall("hello")
    extensions.sync(pip=pip)
    assert [p.name for p in (path / "site").iterdir()] == ["fromenv.installed"], "the site is rebuilt without it"
    with pytest.raises(extensions.ExtensionsError) as info:
        extensions.request_uninstall("fromenv")
    assert info.value.code == "not_installed_from_page"


def test_a_failed_install_keeps_the_previous_set(ext):
    path, pip = ext
    extensions.request_install("good", "good==1")
    extensions.sync(pip=pip)
    extensions.request_install("bad", "boom==1")
    result = extensions.sync(pip=pip)
    assert result["changed"] is False and "no matching distribution" in result["error"]
    assert [p.name for p in (path / "site").iterdir()] == ["good.installed"]
    assert not (path / "site.new").exists()
    assert extensions.state()["last_error"]["error"].startswith("ERROR: no matching")
    assert extensions.pending(), "still wanted, so a restart would try again"


def test_requirements_with_spaces_are_refused(ext):
    with pytest.raises(extensions.ExtensionsError) as info:
        extensions.request_install("x", "x @ git+https://a")
    assert info.value.status == 422


def test_restart_marker(ext):
    import time

    started = time.time() - 1
    assert not extensions.restart_requested_after(started)
    extensions.request_restart()
    assert extensions.restart_requested_after(started)
    assert not extensions.restart_requested_after(time.time() + 1), "a process started after the request does not exit again"


def test_page_installs_can_be_turned_off(ext, monkeypatch):
    monkeypatch.setenv("COMPASS_CATALOG_INSTALL", "off")
    assert not extensions.managed()


def test_catalog_pins_reject_branches_and_spaces():
    ok = ["a==1.2.3", "a@git+https://github.com/x/y.git@v1.2.3#subdirectory=z", "a@git+https://h/x.git@" + "a" * 40,
          "a@https://h/a-1.0-py3-none-any.whl#sha256=" + "0" * 64]
    bad = ["a", "a>=1", "a @ git+https://h/x.git@v1", "a@git+https://h/x.git@main", "a@git+https://h/x.git", "a@https://h/a.whl"]
    assert [catalog.pin_problem(p) for p in ok] == [None] * len(ok)
    assert all(catalog.pin_problem(p) for p in bad)


# ── the API ─────────────────────────────────────────────────────────────────

@pytest.fixture
def client(ext, tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from compass_api.routes import captains as routes

    path = tmp_path / "catalog.json"
    path.write_text(json.dumps({"schema": 1, "captains": [
        {"name": "newbie", "label": "Newbie", "summary": "s", "tier": "open", "version": "0.1.0", "requires_core": ">=0.1,<1", "pip": "newbie==0.1.0"},
        {"name": "paid", "label": "Paid", "summary": "s", "tier": "cloud", "version": "0.1.0", "requires_core": ">=0.1,<1", "pip": None, "homepage": "https://x"},
    ]}))
    monkeypatch.setenv("COMPASS_CATALOG_URL", str(path))
    for name in ("READ_ONLY", "DISABLED_FEATURES"):
        monkeypatch.delenv(name, raising=False)
    catalog.reset_for_tests()
    captains.reset_for_tests()
    monkeypatch.setattr(routes, "get_workspace_config", lambda request: {})
    monkeypatch.setattr(routes, "get_actor_ids", lambda request: {"user_id": "ilia", "workspace_id": "local", "org_id": None})
    exits = []
    monkeypatch.setattr("os._exit", lambda code: exits.append(code))
    app = FastAPI()
    app.include_router(routes.router)
    yield TestClient(app), exits
    catalog.reset_for_tests()


def test_install_from_the_page(client):
    tc, _ = client
    entry = {e["name"]: e for e in tc.get("/api/captains/catalog").json()["entries"]}
    assert entry["newbie"]["installable_from_page"] and not entry["newbie"]["requested"]
    assert entry["paid"]["installable_from_page"] is False

    done = tc.post("/api/captains/install", json={"name": "newbie"})
    assert done.status_code == 200 and done.json()["restart_required"] is True
    assert done.json()["extensions"]["pending_restart"] is True
    entry = {e["name"]: e for e in tc.get("/api/captains/catalog").json()["entries"]}
    assert entry["newbie"]["requested"] and entry["newbie"]["pending_restart"] and entry["newbie"]["removable"]

    paid = tc.post("/api/captains/install", json={"name": "paid"})
    assert paid.status_code == 409 and paid.json()["code"] == "not_installable_vendor"
    assert tc.post("/api/captains/install", json={"name": "nobody"}).status_code == 404

    gone = tc.post("/api/captains/uninstall", json={"name": "newbie"})
    assert gone.status_code == 200 and gone.json()["extensions"]["requested"] == []
    assert tc.post("/api/captains/uninstall", json={"name": "newbie"}).json()["code"] == "not_installed_from_page"


def test_restart_answers_then_exits(client):
    import time

    tc, exits = client
    response = tc.post("/api/captains/restart")
    assert response.status_code == 202 and response.json()["restarting"] is True
    time.sleep(1.3)
    assert exits == [0]
    assert extensions.restart_requested_after(time.time() - 10)


def test_restart_refused_without_a_restart_policy(client, monkeypatch):
    tc, exits = client
    monkeypatch.delenv("COMPASS_SELF_RESTART")
    response = tc.post("/api/captains/restart")
    assert response.status_code == 409 and response.json()["code"] == "restart_unmanaged" and exits == []


def test_install_refused_when_the_page_does_not_manage_extensions(client, monkeypatch):
    tc, _ = client
    monkeypatch.setenv("COMPASS_CATALOG_INSTALL", "off")
    response = tc.post("/api/captains/install", json={"name": "newbie"})
    assert response.status_code == 409 and response.json()["code"] == "extensions_unmanaged"


def test_the_allow_list_limits_page_installs(client, monkeypatch):
    tc, _ = client
    monkeypatch.setenv("COMPASS_CATALOG_ALLOW", "someone_else")
    entry = {e["name"]: e for e in tc.get("/api/captains/catalog").json()["entries"]}
    assert entry["newbie"]["allowed"] is False and entry["newbie"]["installable_from_page"] is False
    refused = tc.post("/api/captains/install", json={"name": "newbie"})
    assert refused.status_code == 403 and refused.json()["code"] == "not_allowed"
    monkeypatch.setenv("COMPASS_CATALOG_ALLOW", "newbie, someone_else")
    assert tc.post("/api/captains/install", json={"name": "newbie"}).status_code == 200
