"""Tests for sensitive-value scrubbing.

`terraform plan -generate-config-out` writes attribute values exactly as AWS
returns them, and these pull requests frequently land in public repositories.
Nothing generated may reach disk, a commit, or a PR body without passing here.
"""

from __future__ import annotations

import pytest

from captains.iac.scrub import SCRUB_MARKER, ScrubError, scrub, sensitive_attributes

SCHEMA = {
    "provider_schemas": {
        "registry.terraform.io/hashicorp/aws": {
            "resource_schemas": {
                "aws_db_instance": {
                    "block": {
                        "attributes": {
                            "password": {"sensitive": True},
                            "identifier": {"sensitive": False},
                        }
                    }
                },
                "aws_security_group": {
                    "block": {"attributes": {"name": {"sensitive": False}}}
                },
            }
        }
    }
}


class TestSensitiveAttributes:
    def test_extracts_only_flagged_attributes(self):
        assert sensitive_attributes(SCHEMA) == {"aws_db_instance": {"password"}}

    def test_tolerates_an_empty_schema(self):
        assert sensitive_attributes({}) == {}


def test_redacts_an_attribute_the_provider_marks_sensitive():
    hcl = 'resource "aws_db_instance" "db" {\n  password   = "hunter2"\n  identifier = "demo"\n}\n'

    result = scrub(hcl, SCHEMA)

    assert "hunter2" not in result.text
    assert SCRUB_MARKER in result.text
    assert 'identifier = "demo"' in result.text
    assert result.redacted == ["aws_db_instance.password"]


def test_removes_the_value_rather_than_masking_it():
    """A masked secret still leaks its length and shape in a public diff."""
    hcl = 'resource "aws_db_instance" "db" {\n  password = "hunter2"\n}\n'

    result = scrub(hcl, SCHEMA)

    assert "*" not in result.text
    assert "hunter2" not in result.text
    assert "# password = (redacted)" in result.text


def test_name_based_floor_catches_what_the_schema_misses():
    """The schema is authoritative, but must not be the only line of defence."""
    hcl = 'resource "aws_something_new" "x" {\n  api_token = "abc123xyz"\n}\n'

    result = scrub(hcl, SCHEMA)

    assert "abc123xyz" not in result.text
    assert result.redacted == ["aws_something_new.api_token"]


@pytest.mark.parametrize(
    "attr",
    ["password", "db_password", "secret_key", "private_key", "auth_header", "session_key"],
)
def test_floor_covers_common_secret_names(attr):
    hcl = f'resource "aws_thing" "x" {{\n  {attr} = "leaked-value-here"\n}}\n'

    assert "leaked-value-here" not in scrub(hcl, None).text


def test_leaves_ordinary_configuration_untouched():
    hcl = 'resource "aws_security_group" "alb" {\n  name   = "public-demo-demo-alb"\n  vpc_id = "vpc-123456"\n}\n'

    result = scrub(hcl, SCHEMA)

    assert result.text == hcl
    assert result.count == 0


def test_fails_closed_for_a_public_repo_without_a_schema():
    """Publishing to a public repo on a name-based guess alone is not acceptable."""
    with pytest.raises(ScrubError, match="provider schema unavailable"):
        scrub('resource "aws_db_instance" "db" {\n  password = "x"\n}\n', None, require_schema=True)


def test_permits_a_missing_schema_when_not_required():
    result = scrub('resource "aws_thing" "x" {\n  name = "fine"\n}\n', None, require_schema=False)

    assert result.schema_used is False
    assert result.count == 0


def test_tracks_the_resource_type_across_blocks():
    """Attribution has to follow the block, or redactions name the wrong type."""
    hcl = (
        'resource "aws_security_group" "alb" {\n  name = "sg"\n}\n'
        'resource "aws_db_instance" "db" {\n  password = "secret-value"\n}\n'
    )

    result = scrub(hcl, SCHEMA)

    assert result.redacted == ["aws_db_instance.password"]
