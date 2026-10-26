"""The jobs routes: a stored job's numbers come back as numbers."""

from __future__ import annotations

from decimal import Decimal

from compass_api.routes.jobs import _summary


def test_a_stored_result_keeps_its_numbers():
    """DynamoDB returns every number as a Decimal and pydantic writes a Decimal as a string;
    the patch tick's counts (and any captain's) must read as integers and floats."""
    job = {"job_id": "j1", "captain": "patch", "command": "tick", "status": "completed", "created_at": Decimal("1761480000000"), "updated_at": Decimal("1761480001000"),
           "result_summary": {"matched": Decimal("1"), "seconds": Decimal("0.25"), "summary": {"open": Decimal("1")}, "prs": [{"number": Decimal("7")}]}}
    out = _summary(job).model_dump(mode="json")
    assert out["result_summary"] == {"matched": 1, "seconds": 0.25, "summary": {"open": 1}, "prs": [{"number": 7}]}
    assert out["created_at"] == 1761480000000 and isinstance(out["created_at"], int)
