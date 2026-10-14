"""Tests for plan interpretation and reference-index selection.

The plan summary is what the verify gate decides on, so misreading it means
either blocking a correct PR or, worse, blessing a destructive one.
"""

from __future__ import annotations

import pytest

from captains.iac.reference_index import (
    MIN_VALUE_LENGTH,
    Reference,
    ReferenceIndex,
    _top_level_local_names,
)
from captains.iac.tf_runner import summarise_plan


def _change(address, actions, importing=False):
    change = {"actions": actions}
    if importing:
        change["importing"] = {"id": "abc"}
    return {"address": address, "change": change}


class TestSummarisePlan:
    def test_a_pure_import_is_clean(self):
        raw = {"resource_changes": [_change("aws_security_group.alb", ["no-op"], importing=True)]}

        summary = summarise_plan(raw)

        assert summary.imports == ["aws_security_group.alb"]
        assert summary.is_import_only

    def test_a_create_is_not_clean(self):
        raw = {"resource_changes": [_change("aws_s3_bucket.new", ["create"])]}

        summary = summarise_plan(raw)

        assert summary.creates == ["aws_s3_bucket.new"]
        assert not summary.is_import_only

    def test_a_replacement_counts_as_a_destroy(self):
        """create+delete is a replacement of live infrastructure.

        Counting it as a create would let the gate pass a plan that destroys a
        security group — the exact failure mode of an omitted immutable
        `description`.
        """
        raw = {"resource_changes": [_change("aws_security_group.alb", ["delete", "create"])]}

        summary = summarise_plan(raw)

        assert summary.destroys == ["aws_security_group.alb"]
        assert summary.creates == []
        assert not summary.is_import_only

    def test_an_update_is_not_clean(self):
        raw = {"resource_changes": [_change("aws_cloudwatch_log_group.lg", ["update"])]}

        assert not summarise_plan(raw).is_import_only

    def test_an_import_that_also_changes_is_not_clean(self):
        """Importing while planning a change means the config does not match reality."""
        raw = {"resource_changes": [_change("aws_security_group.alb", ["update"], importing=True)]}

        summary = summarise_plan(raw)

        assert summary.imports == ["aws_security_group.alb"]
        assert not summary.is_import_only

    def test_an_empty_plan_is_clean(self):
        assert summarise_plan({}).is_import_only


class TestIndexSelection:
    def test_prefers_a_lower_ranked_kind(self):
        idx = ReferenceIndex()
        idx.add("us-east-1", Reference("data.aws_region.current.name", "data"))
        idx.add("us-east-1", Reference("local.aws_region", "local"))

        assert idx.lookup("us-east-1").expression == "local.aws_region"

    def test_prefers_the_shorter_expression_within_a_kind(self):
        idx = ReferenceIndex()
        idx.add("vpc-123456", Reference("module.networking.primary_vpc_id", "module_output"))
        idx.add("vpc-123456", Reference("module.vpc.vpc_id", "module_output"))

        assert idx.lookup("vpc-123456").expression == "module.vpc.vpc_id"

    def test_records_alternatives_without_losing_the_winner(self):
        idx = ReferenceIndex()
        idx.add("us-east-1", Reference("local.aws_region", "local"))
        idx.add("us-east-1", Reference("data.aws_region.current.name", "data"))

        assert idx.lookup("us-east-1").expression == "local.aws_region"
        assert "data.aws_region.current.name" in idx.alternatives("us-east-1")

    def test_a_repeated_identical_reference_is_not_ambiguous(self):
        idx = ReferenceIndex()
        idx.add("vpc-123456", Reference("module.vpc.vpc_id", "module_output"))
        idx.add("vpc-123456", Reference("module.vpc.vpc_id", "module_output"))

        assert idx.ambiguous == {}

    @pytest.mark.parametrize("value", [True, False, None, "", "main", "demo", 80, 1])
    def test_rejects_values_too_generic_to_match_safely(self, value):
        """A short or boolean value matches by coincidence and would rewrite
        unrelated attributes across the file."""
        idx = ReferenceIndex()
        idx.add(value, Reference("var.whatever", "var"))

        assert len(idx) == 0

    def test_accepts_a_region_string(self):
        """The length floor must not exclude values that genuinely want rewiring."""
        assert len("us-east-1") >= MIN_VALUE_LENGTH

        idx = ReferenceIndex()
        idx.add("us-east-1", Reference("local.aws_region", "local"))

        assert idx.lookup("us-east-1") is not None

    def test_rejects_the_terraform_default_description(self):
        """"Managed by Terraform" is the provider's own filler and appears
        identically on unrelated resources."""
        idx = ReferenceIndex()
        idx.add("Managed by Terraform", Reference("var.desc", "var"))

        assert len(idx) == 0

    def test_indexes_numbers_as_strings(self):
        idx = ReferenceIndex()
        idx.add(123456789012, Reference("local.account", "local"))

        assert idx.lookup("123456789012").expression == "local.account"


class TestLocalsParsing:
    def test_collects_top_level_names_only(self):
        text = 'locals {\n  env = "x"\n  images = {\n    a = { b = 1 }\n  }\n  region = "y"\n}\n'

        assert _top_level_local_names(text) == ["env", "images", "region"]

    def test_handles_several_locals_blocks(self):
        text = 'locals {\n  a = 1\n}\n\nlocals {\n  b = 2\n}\n'

        assert _top_level_local_names(text) == ["a", "b"]

    def test_ignores_a_file_without_locals(self):
        assert _top_level_local_names('resource "aws_s3_bucket" "b" {}\n') == []


class TestPlanBaseline:
    def test_subtracts_pre_existing_changes(self):
        """A repo whose main branch already drifts must not have that drift
        attributed to a pull request that arrived into it."""
        baseline = summarise_plan({
            "resource_changes": [
                _change("aws_iam_role.stale", ["delete"]),
                _change("module.svc.aws_ecs_service.api", ["update"]),
            ]
        })
        branch = summarise_plan({
            "resource_changes": [
                _change("aws_iam_role.stale", ["delete"]),
                _change("module.svc.aws_ecs_service.api", ["update"]),
                _change("aws_security_group.alb", ["no-op"], importing=True),
            ]
        })

        attributable = branch.minus(baseline)

        assert attributable.is_import_only
        assert attributable.imports == ["aws_security_group.alb"]

    def test_keeps_changes_the_branch_introduced(self):
        baseline = summarise_plan({"resource_changes": [_change("aws_iam_role.stale", ["delete"])]})
        branch = summarise_plan({
            "resource_changes": [
                _change("aws_iam_role.stale", ["delete"]),
                _change("aws_s3_bucket.oops", ["delete"]),
            ]
        })

        attributable = branch.minus(baseline)

        assert attributable.destroys == ["aws_s3_bucket.oops"]
        assert not attributable.is_import_only

    def test_an_empty_baseline_changes_nothing(self):
        branch = summarise_plan({"resource_changes": [_change("aws_s3_bucket.new", ["create"])]})

        assert branch.minus(summarise_plan({})).creates == ["aws_s3_bucket.new"]
