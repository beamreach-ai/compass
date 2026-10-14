from __future__ import annotations

from compass_api.routes.prs import collect_prs, prs_from_job


def _job(captain, command, status="completed", result=None, payload=None, events=None, **extra):
    return {
        "job_id": f"{captain}-{command}",
        "captain": captain,
        "command": command,
        "status": status,
        "result_summary": result or {},
        "payload": payload or {},
        "events": events or [],
        "created_at": extra.get("created_at", 1),
        "updated_at": 2,
        "error": extra.get("error"),
    }


def test_iac_import_and_fix_drift():
    jobs = [
        _job("iac", "import", result={"pr_url": "https://github.com/acme/infra/pull/7", "resource_count": 3}, payload={"repo": "acme/infra"}, created_at=5),
        _job("iac", "fix_drift", result={"pr_urls": ["https://github.com/acme/a/pull/1", "https://github.com/acme/b/pull/2"], "repos": ["acme/a"]}, created_at=4),
        _job("iac", "import", status="running", payload={"repo": "acme/x", "resource_ids": ["r1", "r2"]}, created_at=3),
        _job("iac", "import", status="completed", result={}, payload={"repo": "acme/none"}, created_at=2),
    ]
    prs = collect_prs(jobs)
    assert [p["type"] for p in prs] == ["iac_import", "fix_drift", "fix_drift", "iac_import"]
    assert prs[0]["pr_url"].endswith("/pull/7") and prs[0]["repo"] == "acme/infra" and prs[0]["resource_count"] == 3
    assert prs[1]["repo"] == "acme/a" and prs[2]["repo"] == "acme/b"
    assert prs[3]["pr_url"] is None and prs[3]["resource_count"] == 2 and prs[3]["job_status"] == "running"


def test_engine_solve_pr_from_events_and_honeypot_from_result():
    engine = _job(
        "engine",
        "solve",
        payload={"finding_id": "prowler:abc"},
        events=[
            {"message": "planning", "data": None},
            {"message": "Pull request opened: https://github.com/acme/infra/pull/9", "data": {"pr_urls": ["https://github.com/acme/infra/pull/9"]}},
        ],
    )
    finops = _job("finops_engine", "solve", status="failed", payload={"finding_id": "finops:1"}, error="boom")
    honeypot = _job("honeypot", "plant", result={"pr_url": "https://github.com/acme/infra/pull/10", "honeytoken_id": "ht1"})
    other = _job("mapper", "run")

    prs = collect_prs([engine, finops, honeypot, other])
    by_type = {p["type"]: p for p in prs}
    assert set(by_type) == {"security_fix", "finops_fix", "honeypot_plant"}
    assert by_type["security_fix"]["pr_url"] == "https://github.com/acme/infra/pull/9"
    assert by_type["security_fix"]["repo"] == "acme/infra" and by_type["security_fix"]["finding_id"] == "prowler:abc"
    assert by_type["finops_fix"]["pr_url"] is None and by_type["finops_fix"]["error"] == "boom"
    assert by_type["honeypot_plant"]["pr_url"].endswith("/pull/10")
    assert prs_from_job(other) == []


def test_captain_filter():
    jobs = [_job("iac", "import", result={"pr_url": "https://github.com/a/b/pull/1"}), _job("honeypot", "plant", result={"pr_url": "https://github.com/a/b/pull/2"})]
    assert [p["captain"] for p in collect_prs(jobs, captains={"iac"})] == ["iac"]
