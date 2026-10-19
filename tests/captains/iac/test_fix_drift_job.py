"""Fix-drift reports what it could not do (found by the e2e suite: a model-free install
'completed' a fix with no pull request and no reason)."""

from __future__ import annotations

import pytest

from captains.iac.fix_drift_job import IACFixDriftJob


def _job(monkeypatch, outcome):
    job = IACFixDriftJob("org", "target", {"workspace_id": "ws"})
    store = type("Store", (), {"get_nodes": lambda self, org, ids: {rid: object() for rid in ids}})()
    monkeypatch.setattr(job, "_load_store", lambda: store)
    monkeypatch.setattr(job, "_batch_lookup_ownership", lambda store, ids: {rid: {"repo": f"acme/{rid}"} for rid in ids})
    monkeypatch.setattr(job, "_fix_drift_in_repo", outcome)
    return job


def test_a_fix_that_made_no_pull_request_fails_with_the_reason(monkeypatch):
    def no_model(repo, items):
        raise RuntimeError("no language model is configured for HCL generation")

    with pytest.raises(RuntimeError, match="acme/a: no language model"):
        _job(monkeypatch, no_model).run(resource_ids=["a"])


def test_a_partial_fix_reports_the_repositories_that_failed(monkeypatch):
    def half(repo, items):
        if repo == "acme/b":
            raise RuntimeError("push refused")
        return f"https://github.com/{repo}/pull/1"

    result = _job(monkeypatch, half).run(resource_ids=["a", "b"])
    assert result["pr_urls"] == ["https://github.com/acme/a/pull/1"] and result["errors"] == {"acme/b": "push refused"}
