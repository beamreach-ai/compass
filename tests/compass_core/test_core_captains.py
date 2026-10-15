"""Captains as extensions: discovery, state, the enqueue and dispatch checks, and the
Captains API on the open app (docs/captain_extensions_spec.md §2, §3, §6.1 O4)."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from compass_core import captain_admin, captains, dispatch, licence, queue
from compass_core import config as core_config
from compass_core.captains import CaptainManifest, TabSpec


@pytest.fixture
def fresh_registry():
    captains.reset_for_tests()
    yield
    captains.reset_for_tests()


def _fake(name="fake", **kw):
    base = dict(name=name, label=name.title(), summary="a test captain", tier="open", feature=name, commands="tests.compass_core.fake_commands")
    base.update(kw)
    return CaptainManifest(**base)


# ── state ───────────────────────────────────────────────────────────────────

def test_default_state_is_everything_on(fresh_registry):
    st = captains.states({})
    assert all(s.active for s in st.values())
    assert st["core"].active and st["core"].manifest.is_core


def test_admin_toggle_deploy_veto_and_dependencies(fresh_registry):
    st = captains.states({"captains": {"iac": {"enabled": False}}})
    assert not st["iac"].active and st["iac"].reason == "disabled_by_admin" and st["iac"].code == "captain_disabled"
    st = captains.states({}, disabled_features={"iac"})
    assert st["iac"].reason == "disabled_by_deploy"
    st = captains.states({"captains": {"mapper": {"enabled": False}}})
    assert st["honeypot"].reason == "dependency_off" and st["honeypot"].blocked_by == ("mapper",) and st["honeypot"].code == "dependency_off"
    assert set(captains.required_by("mapper", {})) == {"librarian", "honeypot", "iac"}


def test_incompatible_manifest_is_listed_but_not_active(fresh_registry):
    captains.register(_fake("old", requires_core=">=99"))
    assert "old" in captains.incompatible()
    st = captains.states({})["old"]
    assert not st.compatible and not st.active and st.code == "captain_incompatible"
    assert "old" not in captains.commands_modules(), "nothing of an incompatible captain is loaded"


def test_register_rejects_a_bad_manifest(fresh_registry):
    with pytest.raises(ValueError, match="lower_snake_case"):
        captains.register(_fake("Bad-Name"))
    with pytest.raises(ValueError, match="tier"):
        captains.register(_fake("x", tier="gold"))


# ── licence ─────────────────────────────────────────────────────────────────

def test_licence_plan_rule_and_claims(fresh_registry):
    cloud = _fake("paid", tier="cloud")
    enterprise = _fake("ent", tier="enterprise")
    free = licence.Licence()
    assert free.is_licensed(cloud), "cloud-tier captains stay usable on the free plan (D22)"
    assert not free.is_licensed(enterprise)
    assert licence.Licence(plan="enterprise").is_licensed(enterprise)
    assert licence.Licence(captains=frozenset({"ent"})).is_licensed(enterprise)
    assert licence.Licence(tiers=frozenset({"enterprise"})).is_licensed(enterprise)
    assert licence.Licence(captains=frozenset({"*"})).is_licensed(enterprise)
    expired = licence.Licence(plan="enterprise", expires_at="2000-01-01T00:00:00Z")
    assert expired.expired and not expired.is_licensed(enterprise)
    assert licence.from_config({"billing": {"plan": "cloud", "licensed_captains": "a,b", "licensed_tiers": ["enterprise"]}}).to_dict()["captains"] == ["a", "b"]


def test_unlicensed_captain_cannot_be_enabled(fresh_registry):
    captains.register(_fake("ent", tier="enterprise"))
    assert captains.states({})["ent"].reason == "unlicensed"
    with pytest.raises(captain_admin.CaptainAdminError) as info:
        captain_admin.set_enabled({}, "ent", True)
    assert info.value.code == "captain_unlicensed" and info.value.status == 402
    assert captains.states({"billing": {"plan": "enterprise"}})["ent"].active


# ── admin edits ─────────────────────────────────────────────────────────────

def test_set_enabled_follows_dependencies(fresh_registry):
    config: dict = {}
    with pytest.raises(captain_admin.CaptainAdminError) as info:
        captain_admin.set_enabled(config, "mapper", False)
    assert info.value.code == "required_by" and set(info.value.names) == {"librarian", "honeypot", "iac"}
    changed = captain_admin.set_enabled(config, "mapper", False, cascade=True, actor="ilia")
    assert set(changed) == {"mapper", "librarian", "honeypot", "iac"}
    assert config["captains"]["mapper"] == {"enabled": False, "disabled_at": config["captains"]["mapper"]["disabled_at"], "disabled_by": "ilia"}
    with pytest.raises(captain_admin.CaptainAdminError) as info:
        captain_admin.set_enabled(config, "iac", True)
    assert info.value.code == "dependency_off" and info.value.names == ("mapper",)
    assert set(captain_admin.set_enabled(config, "iac", True, with_dependencies=True)) == {"mapper", "iac"}
    assert captains.states(config)["iac"].active and not captains.states(config)["honeypot"].active
    with pytest.raises(captain_admin.CaptainAdminError, match="core"):
        captain_admin.set_enabled(config, "core", False)


def test_config_section_is_validated_and_secrets_are_masked(fresh_registry):
    schema = {"type": "object", "properties": {"token": {"type": "string", "x-secret": True}, "n": {"type": "integer", "maximum": 5},
              "nested": {"type": "object", "properties": {"key": {"type": "string", "x-secret": True}}}}}
    m = _fake("sec", config_schema=schema)
    config = {"sec": {"token": "abcdefgh", "n": 1, "nested": {"key": "zz"}}}
    shown = captain_admin.masked_section(config, m)
    assert shown == {"token": "…efgh", "n": 1, "nested": {"key": "…"}}
    captain_admin.apply_section_update(config, m, {"token": "…efgh", "n": 3, "nested": {"key": "…"}})
    assert config["sec"] == {"token": "abcdefgh", "n": 3, "nested": {"key": "zz"}}, "a mask sent back keeps the secret"
    captain_admin.apply_section_update(config, m, {"token": "newtoken"})
    assert config["sec"]["token"] == "newtoken"
    with pytest.raises(captain_admin.CaptainAdminError) as info:
        captain_admin.apply_section_update(config, m, {"n": 9})
    assert info.value.code == "invalid_config" and info.value.status == 422 and "n:" in str(info.value)
    assert config["sec"]["n"] == 3, "an invalid update changes nothing"
    captain_admin.apply_section_update(config, m, {"n": None})
    assert "n" not in config["sec"], "null removes a key"


# ── the checks ──────────────────────────────────────────────────────────────

def test_enqueue_and_dispatch_refuse_a_captain_that_is_off(fresh_registry, monkeypatch):
    monkeypatch.setattr(core_config, "_loader", lambda ws: {"workspace_id": ws, "captains": {"iac": {"enabled": False}}})
    monkeypatch.setattr(dispatch, "_registry", {})
    dispatch.register("iac", {"import": lambda p: {"ok": True}})
    dispatch.register("prs", {"sync": lambda p: {"ok": True}})
    dispatch.register("x", {"go": lambda p: {"ok": True}})
    with pytest.raises(captains.CaptainUnavailable) as info:
        dispatch.dispatch("iac", "import", {"workspace_id": "ws"})
    assert info.value.code == "captain_disabled" and "turned off" in str(info.value)
    assert dispatch.dispatch("prs", "sync", {"workspace_id": "ws"}) == {"ok": True}
    assert dispatch.dispatch("x", "go", {"workspace_id": "ws"}) == {"ok": True}, "unknown captains are not the registry's to refuse"
    inline = queue.InlineQueue(dispatch.dispatch)
    with pytest.raises(captains.CaptainUnavailable):
        inline.enqueue(captain="iac", command="import", payload={"workspace_id": "ws"})
    assert inline.list_jobs() == [], "refused before a job record exists"


# ── the API on the open app ─────────────────────────────────────────────────

@pytest.fixture
def open_client(tmp_path, monkeypatch, fresh_registry):
    cfg = {
        "workspace_id": "local",
        "aws": {"auth_mode": "local_iam", "region": "us-east-1"},
        "github": {},
        "dynamodb_config": {"workspace_id": "local", "items": "compass_items_local"},
    }
    path = tmp_path / "local.json"
    path.write_text(json.dumps(cfg))
    monkeypatch.setenv("COMPASS_CONFIG", str(path))
    for name in ("DISABLED_FEATURES", "DEFAULT_WORKSPACE", "TARGET", "WORKSPACES_TABLE", "APP_USERNAME", "APP_PASSWORD", "READ_ONLY"):
        monkeypatch.setenv(name, "x")
        monkeypatch.delenv(name)
    monkeypatch.setattr(core_config, "_loader", None)
    monkeypatch.setattr(core_config, "_saver", None)
    from compass_api import extensions
    from compass_api import workspace as api_workspace

    for name in ("_bootstrap_sections", "_status_sections"):
        monkeypatch.setattr(extensions, name, {})
    for name in ("_bootstrap_hooks", "_aws_saved_hooks", "_job_restart_guards"):
        monkeypatch.setattr(extensions, name, [])
    monkeypatch.setattr(api_workspace, "_loader", api_workspace._open_loader)
    monkeypatch.setattr(api_workspace, "_saver", api_workspace._open_saver)
    monkeypatch.setattr("compass_api.routes._infra_scan.current_infra_scan_summary", lambda *a, **k: {"scanned": True})
    previous = queue._factory
    from compass_api.app import create_app

    client = TestClient(create_app())
    client.config_path = path  # type: ignore[attr-defined]
    yield client
    queue.set_job_queue(previous)


def _saved(client):
    return json.loads(client.config_path.read_text())


def test_captains_api_lists_toggles_and_hides_tabs(open_client):
    client = open_client
    body = client.get("/api/captains").json()
    names = [c["name"] for c in body["captains"]]
    assert names[0] == "core" and {"mapper", "honeypot", "iac", "prs"} <= set(names)
    assert body["licence"]["plan"] == "free" and body["core_version"] == captains.core_version()
    boot = client.get("/api/settings/bootstrap").json()
    assert "iac" in boot["app"]["enabled_tabs"] and [c["name"] for c in boot["captains"]] == names
    assert {t["key"]: t["group"] for t in boot["app"]["tabs"]}["honeypots"] == "security"

    off = client.post("/api/captains/iac/disable")
    assert off.status_code == 200, off.text
    assert off.json()["changed"] == ["iac"] and off.json()["captain"]["active"] is False
    assert _saved(client)["captains"]["iac"]["enabled"] is False
    boot = client.get("/api/settings/bootstrap").json()
    assert "iac" not in boot["app"]["enabled_tabs"] and "maps" in boot["app"]["enabled_tabs"]

    # work is refused with the reason; reads still answer (whatever they answer)
    scan = client.post("/api/iac/scan", json={})
    assert scan.status_code == 403 and scan.json()["code"] == "captain_disabled" and scan.json()["captain"] == "iac"
    assert client.get("/api/prs/watcher").status_code == 200, "reads of other captains are untouched"
    assert client.get("/api/captains/iac").json()["reason"] == "disabled_by_admin"

    on = client.post("/api/captains/iac/enable")
    assert on.status_code == 200 and on.json()["captain"]["active"] is True
    assert "iac" in client.get("/api/settings/bootstrap").json()["app"]["enabled_tabs"]


def test_captains_api_dependency_rules(open_client):
    client = open_client
    refused = client.post("/api/captains/mapper/disable")
    assert refused.status_code == 409 and refused.json()["code"] == "required_by"
    assert set(refused.json()["captains"]) == {"librarian", "honeypot", "iac"}
    ok = client.post("/api/captains/mapper/disable", json={"cascade": True})
    assert ok.status_code == 200 and set(ok.json()["changed"]) == {"mapper", "librarian", "honeypot", "iac"}
    plant = client.post("/api/honeypots/tokens", json={"honeytoken_class": "db_credential"})
    assert plant.status_code == 403 and plant.json()["code"] == "captain_disabled"
    assert client.post("/api/captains/honeypot/enable").status_code == 409
    back = client.post("/api/captains/honeypot/enable", json={"with_dependencies": True})
    assert back.status_code == 200 and set(back.json()["changed"]) == {"mapper", "honeypot"}
    assert client.post("/api/captains/core/disable").status_code == 400
    assert client.post("/api/captains/nope/disable").status_code == 404


def test_captains_api_config_round_trip(open_client):
    client = open_client
    shown = client.get("/api/captains/prs/config").json()
    assert shown["schema"]["properties"]["watcher"]["properties"]["mode"]["enum"] == ["off", "approve", "autopilot"]
    bad = client.put("/api/captains/prs/config", json={"watcher": {"risk_max": 9}})
    assert bad.status_code == 422 and bad.json()["code"] == "invalid_config"
    good = client.put("/api/captains/prs/config", json={"watcher": {"mode": "approve", "risk_max": 1}})
    assert good.status_code == 200 and good.json()["config"]["watcher"] == {"mode": "approve", "risk_max": 1}
    assert _saved(client)["prs"]["watcher"] == {"mode": "approve", "risk_max": 1}
    assert client.get("/api/prs/watcher").json()["mode"] == "approve", "the captain reads what the generic form wrote"
    assert client.get("/api/captains/nope/config").status_code == 404


# ── the shell ───────────────────────────────────────────────────────────────

def test_core_commands_list_toggle_and_configure(tmp_path, monkeypatch, fresh_registry, capsys):
    from compass_core import cli

    path = tmp_path / "ws.json"
    path.write_text(json.dumps({"workspace_id": "ws", "dynamodb_config": {"workspace_id": "ws"}}))
    monkeypatch.setenv("COMPASS_CONFIG", str(path))
    monkeypatch.setattr(core_config, "_loader", None)
    monkeypatch.setattr(core_config, "_saver", None)
    monkeypatch.setattr(dispatch, "_registry", {})
    previous = queue._factory
    try:
        assert cli.main(["captains", "--workspace", "ws"]) == 0
        out = json.loads(capsys.readouterr().out)
        assert {c["name"] for c in out["captains"]} >= {"core", "iac", "prs"}
        assert cli.main(["disable", "captain=iac", "--workspace", "ws"]) == 0
        assert json.loads(capsys.readouterr().out)["captain"]["active"] is False
        assert json.loads(path.read_text())["captains"]["iac"]["enabled"] is False
        assert cli.main(["disable", "captain=mapper", "--workspace", "ws"]) == 2, "required_by is a message, not a traceback"
        assert "depend on" in capsys.readouterr().err
        assert cli.main(["configure", "captain=prs", 'set={"watcher": {"mode": "approve"}}', "--workspace", "ws"]) == 0
        assert json.loads(capsys.readouterr().out)["config"]["watcher"]["mode"] == "approve"
        assert cli.main(["configure", "captain=prs", 'set={"watcher": {"risk_max": 9}}', "--workspace", "ws"]) == 2
        assert "risk_max" in capsys.readouterr().err
    finally:
        queue.set_job_queue(previous)
