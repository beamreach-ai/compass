from __future__ import annotations

from captains.prs import policy


def _record(**over):
    base = {"state": "open", "watch": {"status": "watching"}, "counters": {"revisions": 0}}
    base.update(over)
    return base


def test_defaults_and_update():
    settings = policy.watcher_settings({})
    assert settings["mode"] == "autopilot" and settings["risk_max"] == 2 and settings["max_revisions"] == 3
    assert settings["auto_merge"]["enabled"] is False and settings["auto_merge"]["risk_max"] == 1
    config = {}
    updated = policy.apply_settings_update(config, {"mode": "approve", "risk_max": 9, "auto_merge": {"enabled": True, "repos": ["acme/infra"]}})
    assert updated["mode"] == "approve" and updated["risk_max"] == 5
    assert updated["auto_merge"]["enabled"] is True and updated["auto_merge"]["repos"] == ["acme/infra"]
    assert config["prs"]["watcher"]["mode"] == "approve"
    assert policy.watcher_settings({"prs": {"watcher": {"mode": "bogus"}}})["mode"] == "autopilot"


def test_revision_risk():
    intent = {"risk": 1, "scope": {"files": ["modules/storage/main.tf"]}}
    assert policy.revision_risk(intent, ["modules/storage/main.tf"])["risk"] == 1
    assert policy.revision_risk(intent, ["modules/storage/other.tf"])["risk"] == 2
    assert policy.revision_risk(intent, ["modules/iam/roles.tf"])["risk"] == 3
    assert policy.revision_risk({}, [])["risk"] == 2


def test_decide(monkeypatch):
    monkeypatch.delenv("READ_ONLY", raising=False)
    settings = policy.watcher_settings({})
    assert policy.decide(settings, risk=2, record=_record(), approved=False, trigger="review")["decision"] == "run"
    assert policy.decide(settings, risk=3, record=_record(), approved=False, trigger="review")["decision"] == "wait_for_approval"
    assert policy.decide(settings, risk=3, record=_record(), approved=True, trigger="review")["decision"] == "run"
    approve = policy.watcher_settings({"prs": {"watcher": {"mode": "approve"}}})
    assert policy.decide(approve, risk=1, record=_record(), approved=False, trigger="review")["decision"] == "wait_for_approval"
    off = policy.watcher_settings({"prs": {"watcher": {"mode": "off"}}})
    assert policy.decide(off, risk=1, record=_record(), approved=False, trigger="review")["decision"] == "recommend_only"
    capped = _record(counters={"revisions": 3})
    assert policy.decide(settings, risk=1, record=capped, approved=False, trigger="review") == {"decision": "blocked", "reason": "max_revisions"}
    assert policy.decide(settings, risk=1, record=capped, approved=False, trigger="manual")["decision"] == "run"
    assert policy.decide(settings, risk=1, record=_record(watch={"status": "needs_human"}), approved=True, trigger="manual")["reason"] == "watch_needs_human"
    assert policy.decide(settings, risk=1, record=_record(state="merged"), approved=False, trigger="review")["reason"] == "pr_not_open"
    monkeypatch.setenv("READ_ONLY", "1")
    assert policy.decide(settings, risk=1, record=_record(), approved=True, trigger="manual")["reason"] == "read_only"
