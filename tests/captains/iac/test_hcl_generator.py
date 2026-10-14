"""Regression tests for the IAC import HCL generator.

These exist because of a data-loss bug: the generator used to name its output
`<module_path>/main.tf`, and the PR writer replaces a file's whole contents.
An import into `environments/demo` therefore overwrote that environment's
hand-written root module — backend, provider, locals and every module block —
with a handful of generated resource stubs.
"""

from __future__ import annotations

import pytest

from captains.iac.hcl_generator import HCLGenerator
from infra_map.models import ResourceNode


def _node(resource_id: str, aws_type: str, name: str) -> ResourceNode:
    return ResourceNode(
        org_id="org-1",
        resource_id=resource_id,
        type=aws_type,
        account_id="123456789012",
        region="us-east-1",
        name=name,
    )


RESOURCES = [
    _node("sg-0b76178d978eabd39", "ec2_security_group", "public-demo-demo-alb"),
    _node(
        "arn:aws:dynamodb:us-east-1:123456789012:table/public-demo-tf-locks",
        "dynamodb_table",
        "public-demo-tf-locks",
    ),
]


@pytest.fixture
def generator(monkeypatch):
    """A generator whose Bedrock call is stubbed out.

    Only the file layout and the import blocks are under test here; the
    resource-block body is the model's output and is not asserted on.
    """
    monkeypatch.setattr(
        HCLGenerator,
        "__init__",
        lambda self, target=None, config=None: None,
    )
    gen = HCLGenerator()
    monkeypatch.setattr(
        HCLGenerator,
        "_generate_resource_hcl_with_claude",
        lambda self, **kwargs: 'resource "aws_security_group" "x" {}\n',
    )
    return gen


def test_never_writes_to_main_tf(generator):
    """The whole point: generated output must not land on an existing file."""
    files = generator.generate(RESOURCES, module_path="environments/demo")

    assert files, "expected generated files"
    for path in files:
        assert not path.endswith("main.tf"), f"{path} would overwrite a hand-maintained file"


def test_files_land_under_the_module_path(generator):
    files = generator.generate(RESOURCES, module_path="environments/demo")

    assert set(files) == {
        "environments/demo/beamreach_imports.tf",
        "environments/demo/beamreach_imported.tf",
    }


def test_files_land_at_the_repo_root_when_no_module_path(generator):
    files = generator.generate(RESOURCES, module_path=None)

    assert set(files) == {"beamreach_imports.tf", "beamreach_imported.tf"}


@pytest.mark.parametrize("module_path", ["environments/demo", "./environments/demo/", "environments//demo"])
def test_module_path_is_normalised(generator, module_path):
    files = generator.generate(RESOURCES, module_path=module_path)

    assert "environments/demo/beamreach_imports.tf" in files


def test_import_addresses_carry_no_module_prefix(generator):
    """A filesystem path is not a Terraform module address.

    `environments/demo` holds its own backend and provider, making it a root
    module whose resources are addressed bare. The old code emitted
    `module.environments.module.demo.aws_security_group.…`, which referred to
    modules that do not exist and failed at plan time.
    """
    files = generator.generate(RESOURCES, module_path="environments/demo")
    imports = files["environments/demo/beamreach_imports.tf"]

    assert "module." not in imports
    assert "to = aws_security_group.public_demo_demo_alb" in imports
    assert "to = aws_dynamodb_table.public_demo_tf_locks" in imports


def test_import_blocks_reference_the_resource_file(generator):
    """The two files are a pair; the import file has to say so, because the
    correct cleanup is to delete it and keep the other."""
    files = generator.generate(RESOURCES, module_path="environments/demo")

    assert "beamreach_imported.tf" in files["environments/demo/beamreach_imports.tf"]


def test_every_resource_gets_an_import_block(generator):
    files = generator.generate(RESOURCES, module_path="environments/demo")
    imports = files["environments/demo/beamreach_imports.tf"]

    assert imports.count("import {") == len(RESOURCES)


@pytest.mark.parametrize(
    "resource_id,aws_type,expected_id",
    [
        # A log group imports by name. The scanner records the ARN with the
        # trailing :* wildcard, which Terraform rejects.
        (
            "arn:aws:logs:us-east-1:123456789012:log-group:/aws/lambda/public-demo-map-publisher:*",
            "cloudwatch_log_group",
            "/aws/lambda/public-demo-map-publisher",
        ),
        # A table imports by table name, not ARN.
        (
            "arn:aws:dynamodb:us-east-1:123456789012:table/public-demo-tf-locks",
            "dynamodb_table",
            "public-demo-tf-locks",
        ),
        ("arn:aws:iam::123456789012:role/MapperRole", "iam_role", "MapperRole"),
        (
            "arn:aws:lambda:us-east-1:123456789012:function:public-demo-map-publisher",
            "lambda_function",
            "public-demo-map-publisher",
        ),
        # A security group's ID already *is* the import ID — must pass through.
        ("sg-0b76178d978eabd39", "ec2_security_group", "sg-0b76178d978eabd39"),
        # A load balancer genuinely imports by ARN — must pass through.
        (
            "arn:aws:elasticloadbalancing:us-east-1:123456789012:loadbalancer/app/demo/abc123",
            "elbv2_load_balancer",
            "arn:aws:elasticloadbalancing:us-east-1:123456789012:loadbalancer/app/demo/abc123",
        ),
    ],
)
def test_import_ids_are_normalised_per_resource_type(generator, resource_id, aws_type, expected_id):
    """Terraform's import ID is type-specific and often not the ARN."""
    files = generator.generate([_node(resource_id, aws_type, "thing")], module_path="x")

    assert f'id = "{expected_id}"' in files["x/beamreach_imports.tf"]


def test_unimportable_types_are_skipped_not_invented(generator):
    """`aws_dynamodb_stream` does not exist in any provider.

    The old fallback built a type name from the AWS type, emitting HCL for a
    resource Terraform cannot resolve.
    """
    stream = _node(
        "arn:aws:dynamodb:us-east-1:123456789012:table/public-demo-map-events/stream/2025-12-23T22:50:25.152",
        "dynamodb_stream",
        "public-demo-map-events",
    )
    files = generator.generate([*RESOURCES, stream], module_path="environments/demo")

    imports = files["environments/demo/beamreach_imports.tf"]
    assert "aws_dynamodb_stream" not in imports
    assert imports.count("import {") == len(RESOURCES)

    # Skipping has to be visible, or the resource silently vanishes.
    resources_file = files["environments/demo/beamreach_imported.tf"]
    assert "Not imported:" in resources_file
    assert "public-demo-map-events" in resources_file


def test_unmapped_aws_type_is_skipped(generator):
    files = generator.generate(
        [*RESOURCES, _node("whatever", "some_future_service", "mystery")],
        module_path="x",
    )

    assert "aws_some_future_service" not in files["x/beamreach_imports.tf"]
    assert "mystery" in files["x/beamreach_imported.tf"]


def test_raises_when_nothing_is_importable(generator):
    """Better a clear failure than a PR containing only comments."""
    stream = _node("arn:aws:dynamodb:us-east-1:1:table/t/stream/x", "dynamodb_stream", "s")

    with pytest.raises(RuntimeError, match="None of the selected resources can be imported"):
        generator.generate([stream], module_path="x")
