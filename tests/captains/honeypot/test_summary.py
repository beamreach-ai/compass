"""HP-09: incident facts, summary parsing, report rendering."""

from __future__ import annotations

import json
import re

from captains.honeypot import report, summary

SVC = "arn:aws:ecs:eu-central-1:111122223333:service/demo/web"
OLD = "arn:aws:ecs:eu-central-1:111122223333:task/demo/aaaa1111"
NEW = "arn:aws:ecs:eu-central-1:111122223333:task/demo/bbbb2222"


def incident(repeat=1):
    return {
        "incident_id": "1790000000000-abcd1234", "status": "contained", "alert_count": 2, "repeat_count": repeat,
        "related_incident_id": "1789000000000-ffff0000" if repeat > 1 else None,
        "honeytoken_ids": ["tok-1", "tok-2"], "service_arn": SVC, "source_task_id": "aaaa1111", "source_ip": "203.0.113.7",
        "placement": {"service_arn": SVC, "container": "app", "secret_field": "SHOULD-NOT-LEAK"},
        "first_event_time": "2026-10-06T12:00:00+00:00", "first_received_at": "2026-10-06T12:00:03+00:00",
        "first_alert_sent_at": "2026-10-06T12:00:07+00:00", "action_verified_at": "2026-10-06T12:01:00+00:00",
        "blast_radius": {"secrets": 4, "buckets": ["b1", "b2"]}, "correlation": None, "summary": None,
        "raw_event": {"secretAccessKey": "SHOULD-NOT-LEAK"},
    }


ALERTS = [
    {"alert_id": "a1", "honeytoken_id": "tok-1", "honeytoken_name": "prod/db-password", "honeytoken_class": "secret",
     "source_ip": "203.0.113.7", "ip_type": "external", "access_key_id": "AKIASHOULDNOTLEAK", "raw_event": {"x": 1}},
    {"alert_id": "a2", "honeytoken_id": "tok-2"},
]
AUDITS = [{
    "audit_id": "1-a", "action_type": "ecs_restart_task", "status": "verified", "snapshot": {"taken_at": "t"},
    "executed": {"stopped_task": OLD}, "verify": {"ok": True, "new_task": NEW}, "rollback": {"supported": False},
}]


def test_facts_keys_are_the_allow_list_and_exclude_secrets():
    facts = summary.build_incident_facts(incident(), ALERTS, AUDITS)
    assert set(facts) == set(summary.FACT_KEYS)
    blob = json.dumps(facts)
    assert "SHOULD-NOT-LEAK" not in blob and "AKIA" not in blob
    assert facts["timeline"]["seconds_after_first_event"] == {"received": 3.0, "alert_sent": 7.0, "action_verified": 60.0}
    assert facts["honeytokens"][0] == {"name": "prod/db-password", "class": "secret"}
    a = facts["actions"][0]
    assert (a["old_task_id"], a["new_task_id"], a["verified"], a["snapshot_taken"]) == ("aaaa1111", "bbbb2222", True, True)
    assert facts["workload"] == {"service": "web", "container": "app", "task_id": "aaaa1111"}
    assert "recommended" not in facts["repeat"]
    assert summary.build_incident_facts(incident(2), ALERTS, AUDITS)["repeat"]["recommended"] == summary.RECOMMENDATION


def test_summarize_parses_json_and_fenced_json():
    facts = summary.build_incident_facts(incident(), ALERTS, AUDITS)
    seen = {}

    def plain(prompt, facts_json):
        seen["prompt"], seen["facts"] = prompt, facts_json
        return json.dumps({"paragraph": "p", "bullets": ["a", "b", "c"]})

    out = summary.summarize_incident(facts, invoke=plain)
    assert out["paragraph"] == "p" and out["bullets"] == ["a", "b", "c"] and out["generated_at"] and out["model"]
    assert "strict JSON" in seen["prompt"] and json.loads(seen["facts"]) == facts

    fenced = summary.summarize_incident(facts, invoke=lambda p, f: '```json\n{"paragraph": "x", "bullets": ["y"]}\n```')
    assert fenced["paragraph"] == "x"


def test_unusable_output_raises():
    import pytest

    with pytest.raises(ValueError):
        summary.summarize_incident({}, invoke=lambda p, f: "no json here")


def test_no_hallucinated_facts():
    facts = summary.build_incident_facts(incident(), ALERTS, AUDITS)
    facts_json = json.dumps(facts)

    def echo(prompt, fj):
        f = json.loads(fj)
        t = f["timeline"]["seconds_after_first_event"]
        return json.dumps({
            "paragraph": f"{len(f['honeytokens'])} honeytokens tripped on {f['workload']['service']} from {f['source']['ip']}.",
            "bullets": [f"Alert sent {t['alert_sent']}s after the first event.", f"Task {f['actions'][0]['old_task_id']} replaced by {f['actions'][0]['new_task_id']}."],
        })

    out = summary.summarize_incident(facts, invoke=echo)
    text = out["paragraph"] + " " + " ".join(out["bullets"])
    numbers = re.findall(r"\d+(?:\.\d+)?", text)
    assert numbers
    ip_and_ids = re.findall(r"arn:[\w:/-]+|\b\d+\.\d+\.\d+\.\d+\b|\b[a-f0-9]{8}\b", text)
    for token in numbers + ip_and_ids:
        assert token in facts_json, token


def test_report_markdown_sections_and_pending():
    md = report.render_markdown(incident(), ALERTS, AUDITS, None)
    for heading in ("# Honeytoken incident", "## Summary", "## Timeline", "## Honeytokens", "## Workload", "## Source", "## Blast radius", "## Actions and audit"):
        assert heading in md
    assert "Summary pending." in md and "| Alert sent |" in md and "+7.0s" in md
    assert "snapshot taken: yes" in md and "`aaaa1111` -> `bbbb2222`" in md
    assert "1790000000000-abcd1234" in md.splitlines()[-1]
    assert "SHOULD-NOT-LEAK" not in md and "AKIA" not in md
    assert "Repeat on the same service" not in md


def test_report_with_summary_and_repeat_note():
    s = {"paragraph": "Para.", "bullets": ["One", "Two", "Three"]}
    md = report.render_markdown(incident(2), ALERTS, AUDITS, s, prior_restarts=1)
    assert "Para." in md and "- Two" in md and "Summary pending" not in md
    assert "Repeat on the same service: 2 attempts, 2 restarts. Recommended: isolate the service (not enabled)" in md
    isolate = [{"action_type": "ecs_isolate_service", "status": "verified"}]
    assert "Recommended" not in report.render_markdown(incident(2), ALERTS, AUDITS + isolate, s)
