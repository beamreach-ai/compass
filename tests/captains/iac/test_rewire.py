"""Tests for the literal -> reference rewiring pass.

Rewiring is the step that turns provider-generated configuration into something
that looks hand-written. It is deliberately deterministic rather than
model-driven, because a substitution that is wrong produces config which plans
cleanly against the wrong resource.
"""

from __future__ import annotations

import pytest

from captains.iac.reference_index import Reference, ReferenceIndex
from captains.iac.rewire import resource_addresses, revert_attribute, rewire


@pytest.fixture
def index():
    idx = ReferenceIndex()
    idx.add("vpc-0e040db1f49390291", Reference("module.beamreach-demo-vpc.vpc_id", "module_output"))
    idx.add("us-east-1", Reference("data.aws_region.current.name", "data"))
    idx.add("us-east-1", Reference("local.aws_region", "local"))
    idx.add("public-demo-demo-alb", Reference("aws_lb.demo.name", "resource"))
    return idx


SG = '''resource "aws_security_group" "public_demo_demo_alb" {
  name        = "public-demo-demo-alb"
  vpc_id      = "vpc-0e040db1f49390291"
  region      = "us-east-1"
  description = "ALB security group"
}
'''


def test_rewires_a_matching_literal(index):
    result = rewire(SG, index)

    assert "vpc_id      = module.beamreach-demo-vpc.vpc_id" in result.text
    assert '"vpc-0e040db1f49390291"' not in result.text


def test_prefers_a_local_over_a_data_source(index):
    """Both are correct for us-east-1; the repo's own local reads better."""
    result = rewire(SG, index)

    assert "region      = local.aws_region" in result.text


def test_reports_the_alternatives_it_did_not_choose(index):
    result = rewire(SG, index)

    report = "\n".join(result.report_lines())
    assert "local.aws_region" in report
    assert "data.aws_region.current.name" in report


def test_leaves_identity_attributes_literal(index):
    """`name` is indexed here, and must still not be rewritten.

    Rewriting what a resource *is* rather than what it points at makes the diff
    unreadable and decouples the block from the import that targets it.
    """
    result = rewire(SG, index)

    assert 'name        = "public-demo-demo-alb"' in result.text
    assert result.skipped_identity


def test_leaves_unmatched_values_alone(index):
    result = rewire(SG, index)

    assert 'description = "ALB security group"' in result.text


def test_never_rewrites_a_protected_import_id(index):
    """A block often contains the very id it is being imported by."""
    idx = ReferenceIndex()
    idx.add("sg-0b76178d978eabd39", Reference("aws_security_group.other.id", "resource"))
    hcl = '  security_group_id = "sg-0b76178d978eabd39"\n'

    result = rewire(hcl, idx, protected_values=["sg-0b76178d978eabd39"])

    assert '"sg-0b76178d978eabd39"' in result.text
    assert result.count == 0


def test_counts_only_actual_substitutions(index):
    result = rewire(SG, index)

    assert result.count == 2  # vpc_id and region; name and description untouched


def test_preserves_indentation_and_trailing_newline(index):
    result = rewire(SG, index)

    assert result.text.endswith("}\n")
    assert "\n  vpc_id      = module" in result.text


def test_is_a_no_op_against_an_empty_index():
    result = rewire(SG, ReferenceIndex())

    assert result.text == SG
    assert result.count == 0


def test_ignores_multiline_and_heredoc_values(index):
    """Whole-value matching cannot apply, and a partial edit could corrupt them."""
    hcl = 'policy = <<EOT\nvpc-0e040db1f49390291\nEOT\n'

    result = rewire(hcl, index)

    assert result.text == hcl


class TestRevertAttribute:
    def test_restores_a_literal(self):
        hcl = "  vpc_id = module.beamreach-demo-vpc.vpc_id\n"

        reverted = revert_attribute(hcl, "vpc_id", "vpc-0e040db1f49390291")

        assert reverted == '  vpc_id = "vpc-0e040db1f49390291"\n'

    def test_leaves_an_already_literal_attribute_alone(self):
        hcl = '  vpc_id = "vpc-123"\n'

        assert revert_attribute(hcl, "vpc_id", "vpc-999") == hcl


class TestResourceAddresses:
    def test_collects_resource_and_data_addresses(self):
        hcl = (
            'resource "aws_security_group" "alb" {\n}\n'
            'data "aws_region" "current" {\n}\n'
        )

        assert resource_addresses(hcl) == ["aws_security_group.alb", "data.aws_region.current"]
