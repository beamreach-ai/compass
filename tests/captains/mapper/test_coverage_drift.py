"""Drift is a difference in value, not in how AWS and Terraform spell the same value."""
from captains.mapper.coverage_drift import _coerce


def test_a_number_aws_returns_as_text_is_the_same_number():
    # an SQS queue's VisibilityTimeout comes back as "30"; the state holds 30: the first
    # install's only "drift" was this one, and the fix pull request changed nothing
    assert _coerce("30") == _coerce(30)
    assert _coerce(" 30 ") == _coerce(30.0)
    assert _coerce("31") != _coerce(30)


def test_text_stays_text_and_booleans_stay_booleans():
    assert _coerce("PAY_PER_REQUEST") == "pay_per_request"
    assert _coerce(True) is True
    assert _coerce("python3.12") == "python3.12"
