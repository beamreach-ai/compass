"""Tests for cleaning up terraform-generated configuration.

`-generate-config-out` is faithful to what AWS returned, not to what the
provider will accept. Everything here exists because real generated output
contained something that would not plan.
"""

from __future__ import annotations

from captains.iac.normalize import (
    drop_lines,
    drop_nulls,
    drop_read_only,
    invalid_lines,
    read_only_attributes,
)

SCHEMA = {
    "provider_schemas": {
        "registry.terraform.io/hashicorp/aws": {
            "resource_schemas": {
                "aws_security_group": {
                    "block": {
                        "attributes": {
                            # Computed and not optional -> genuinely read-only.
                            "tags_all": {"computed": True},
                            "arn": {"computed": True},
                            # Computed *and* optional -> "set it or I derive it".
                            "name": {"computed": True, "optional": True},
                            "description": {"optional": True},
                        }
                    }
                }
            }
        }
    }
}


class TestReadOnlyAttributes:
    def test_flags_computed_only_attributes(self):
        assert read_only_attributes(SCHEMA) == {"aws_security_group": {"tags_all", "arn"}}

    def test_does_not_flag_computed_optional_attributes(self):
        """Most optional arguments are also computed — that means the provider
        derives a default, not that the argument is rejected."""
        assert "name" not in read_only_attributes(SCHEMA)["aws_security_group"]


def test_drops_read_only_attributes():
    hcl = (
        'resource "aws_security_group" "alb" {\n'
        '  name     = "public-demo-demo-alb"\n'
        '  tags_all = { Environment = "demo" }\n'
        '  arn      = "arn:aws:ec2:::sg/sg-1"\n'
        '}\n'
    )

    result = drop_read_only(hcl, SCHEMA)

    assert "tags_all" not in result.text
    assert "arn " not in result.text
    assert 'name     = "public-demo-demo-alb"' in result.text
    assert sorted(result.dropped) == ["aws_security_group.arn", "aws_security_group.tags_all"]


def test_leaves_multiline_blocks_alone():
    """A block-opening assignment has continuation lines; deleting only the
    first would orphan the rest and produce unparseable HCL."""
    hcl = (
        'resource "aws_security_group" "alb" {\n'
        '  tags_all = {\n'
        '    Environment = "demo"\n'
        '  }\n'
        '}\n'
    )

    result = drop_read_only(hcl, SCHEMA)

    assert result.text == hcl
    assert result.dropped == []


def test_drops_null_assignments():
    hcl = (
        'resource "aws_security_group" "alb" {\n'
        '  name        = "alb"\n'
        '  name_prefix = null\n'
        '  description = "real"\n'
        '}\n'
    )

    result = drop_nulls(hcl)

    assert "name_prefix" not in result.text
    assert 'description = "real"' in result.text
    assert result.count == 1


def test_ignores_attributes_outside_a_resource_block():
    hcl = 'locals {\n  tags_all = "not a resource attribute"\n}\n'

    assert drop_read_only(hcl, SCHEMA).text == hcl


class TestDropLines:
    def test_removes_the_reported_line(self):
        """This is how `recovery_period_in_days = 0` is removed: terraform
        reports the line, and that exact line goes."""
        hcl = "a = 1\nrecovery_period_in_days = 0\nb = 2\n"

        result = drop_lines(hcl, [2])

        assert result.text == "a = 1\nb = 2\n"
        assert result.dropped == ["recovery_period_in_days = 0"]

    def test_removes_several_and_keeps_order(self):
        hcl = "one\ntwo\nthree\nfour\n"

        assert drop_lines(hcl, [1, 3]).text == "two\nfour\n"

    def test_is_a_no_op_for_no_lines(self):
        hcl = "one\ntwo\n"

        assert drop_lines(hcl, []).text == hcl


class TestInvalidLines:
    def test_extracts_lines_for_the_named_file(self):
        diagnostics = [
            {"range": {"filename": "generated.tf", "start": {"line": 12}}},
            {"range": {"filename": "generated.tf", "start": {"line": 40}}},
            {"range": {"filename": "provider.tf", "start": {"line": 3}}},
        ]

        assert invalid_lines(diagnostics, "generated.tf") == [12, 40]

    def test_deduplicates(self):
        diagnostics = [
            {"range": {"filename": "generated.tf", "start": {"line": 7}}},
            {"range": {"filename": "generated.tf", "start": {"line": 7}}},
        ]

        assert invalid_lines(diagnostics, "generated.tf") == [7]

    def test_tolerates_diagnostics_without_a_range(self):
        """Not every error is attached to a line; those cannot be fixed by
        deletion and must not crash the pass."""
        assert invalid_lines([{"summary": "something broad"}], "generated.tf") == []
