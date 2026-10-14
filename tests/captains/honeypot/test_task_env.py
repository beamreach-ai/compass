"""HP-05: the plant PR puts the honeytoken into an existing task definition's environment."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from captains.honeypot.task_env import (
    TaskEnvError,
    env_entries,
    env_names,
    find_task_definition,
    inject_env,
    remove_env,
    task_definitions,
)

# The shape of the replay workload's task definition (environments/demo/replay.tf).
REPLAY = '''
resource "aws_ecs_task_definition" "web" {
  family                   = "${local.env}-web"
  requires_compatibilities = ["FARGATE"]

  container_definitions = jsonencode([
    {
      name      = "web"
      image     = "${module.replay.web_repository_url}:latest"
      essential = true
      portMappings = [
        {
          containerPort = 3000
          hostPort      = 3000
        }
      ]
      environment = [
        { name = "NODE_ENV", value = "production" },
        { name = "PORT", value = "3000" },
        { name = "UPLOADS_BUCKET", value = module.replay.bucket_names[0] }
      ]
      secrets = [
        { name = "DATABASE_URL", valueFrom = module.replay.secret_arns["prod/db-main"] }
      ]
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-stream-prefix" = "web"
        }
      }
    }
  ])
}

resource "aws_ecs_service" "web" {
  name            = "web"
  task_definition = aws_ecs_task_definition.web.arn
}
'''

DB = env_entries("db_credential", "svc_svc_backup_reporting")
KEY = env_entries("aws_credential", "svc_ci_artifacts_reader")


def _container(text: str, name: str = "web") -> str:
    """Still valid HCL, and the task definition is still found by hcl2."""
    defs = task_definitions(text)
    assert name in defs and defs[name].startswith("${jsonencode(")
    return defs[name]


def test_env_entries_wire_module_outputs_and_never_literal_secrets():
    assert DB == [
        ("DB_HOST", "module.svc_svc_backup_reporting.host"),
        ("DB_PORT", '"5432"'),
        ("DB_USER", "module.svc_svc_backup_reporting.user"),
        ("DB_PASSWORD", "module.svc_svc_backup_reporting.password"),
    ]
    assert KEY == [
        ("BACKUP_AWS_ACCESS_KEY_ID", "module.svc_ci_artifacts_reader.access_key_id"),
        ("BACKUP_AWS_SECRET_ACCESS_KEY", "module.svc_ci_artifacts_reader.secret_access_key"),
    ]
    assert env_entries("s3_object", "x") == []


def test_inject_is_a_minimal_diff_inside_the_container():
    out = inject_env(REPLAY, resource="web", container="web", entries=DB)
    assert env_names(out, resource="web", container="web") == [
        "NODE_ENV", "PORT", "UPLOADS_BUCKET", "DB_HOST", "DB_PORT", "DB_USER", "DB_PASSWORD",
    ]
    old, new = REPLAY.splitlines(), out.splitlines()
    added = [l for l in new if l not in old]
    removed = [l for l in old if l not in new]
    assert removed == ['        { name = "UPLOADS_BUCKET", value = module.replay.bucket_names[0] }']
    assert added[0] == '        { name = "UPLOADS_BUCKET", value = module.replay.bucket_names[0] },'
    assert added[1:] == [
        '        { name = "DB_HOST", value = module.svc_svc_backup_reporting.host },',
        '        { name = "DB_PORT", value = "5432" },',
        '        { name = "DB_USER", value = module.svc_svc_backup_reporting.user },',
        '        { name = "DB_PASSWORD", value = module.svc_svc_backup_reporting.password }',
    ]
    _container(out)  # still valid HCL


def test_two_tokens_then_remove_one_then_the_other_restores_the_file():
    both = inject_env(inject_env(REPLAY, resource="web", container="web", entries=DB), resource="web", container="web", entries=KEY)
    assert env_names(both, resource="web", container="web")[-2:] == ["BACKUP_AWS_ACCESS_KEY_ID", "BACKUP_AWS_SECRET_ACCESS_KEY"]
    one = remove_env(both, resource="web", container="web", names=[v for v, _ in DB])
    assert env_names(one, resource="web", container="web") == ["NODE_ENV", "PORT", "UPLOADS_BUCKET", "BACKUP_AWS_ACCESS_KEY_ID", "BACKUP_AWS_SECRET_ACCESS_KEY"]
    none = remove_env(one, resource="web", container="web", names=[v for v, _ in KEY])
    assert none == REPLAY


def test_reinject_is_idempotent():
    once = inject_env(REPLAY, resource="web", container="web", entries=DB)
    assert inject_env(once, resource="web", container="web", entries=DB) == once


def test_container_without_environment_gets_one():
    text = REPLAY.replace(
        '''      environment = [
        { name = "NODE_ENV", value = "production" },
        { name = "PORT", value = "3000" },
        { name = "UPLOADS_BUCKET", value = module.replay.bucket_names[0] }
      ]
''', "")
    out = inject_env(text, resource="web", container="web", entries=KEY)
    assert env_names(out, resource="web", container="web") == ["BACKUP_AWS_ACCESS_KEY_ID", "BACKUP_AWS_SECRET_ACCESS_KEY"]
    _container(out)


def test_single_line_lists_and_middle_removal():
    text = REPLAY.replace(
        '''environment = [
        { name = "NODE_ENV", value = "production" },
        { name = "PORT", value = "3000" },
        { name = "UPLOADS_BUCKET", value = module.replay.bucket_names[0] }
      ]''', 'environment = [{ name = "A", value = "1" }, { name = "B", value = "2" }, { name = "C", value = "3" }]')
    assert remove_env(text, resource="web", container="web", names=["B"]).count('environment = [{ name = "A", value = "1" }, { name = "C", value = "3" }]') == 1
    assert 'environment = [{ name = "A", value = "1" }, { name = "B", value = "2" }]' in remove_env(text, resource="web", container="web", names=["C"])


def test_only_the_named_container_is_touched():
    two = REPLAY.replace('''      logConfiguration = {''', '''      logConfiguration = {''', 1).replace(
        "  ])\n}", '''    ,
    {
      name        = "sidecar"
      image       = "envoy"
      environment = [{ name = "X", value = "1" }]
    }
  ])
}''', 1)
    out = inject_env(two, resource="web", container="sidecar", entries=KEY)
    assert env_names(out, resource="web", container="web") == ["NODE_ENV", "PORT", "UPLOADS_BUCKET"]
    assert env_names(out, resource="web", container="sidecar") == ["X", "BACKUP_AWS_ACCESS_KEY_ID", "BACKUP_AWS_SECRET_ACCESS_KEY"]


@pytest.mark.parametrize("expr", ['file("defs.json")', "var.container_definitions", 'templatefile("defs.tpl", {})', "local.defs"])
def test_computed_container_definitions_are_refused_with_instructions(expr):
    text = REPLAY.split("container_definitions = ")[0] + f"container_definitions = {expr}\n}}\n"
    with pytest.raises(TaskEnvError, match="not a literal jsonencode"):
        inject_env(text, resource="web", container="web", entries=KEY)


def test_find_by_container_and_by_address():
    files = {"environments/demo/replay.tf": REPLAY, "environments/demo/main.tf": 'locals {\n  env = "x"\n}\n'}
    assert find_task_definition(files, container="web") == ("environments/demo/replay.tf", "web")
    assert find_task_definition(files, container="other", tf_address="aws_ecs_task_definition.web") == ("environments/demo/replay.tf", "web")
    with pytest.raises(TaskEnvError, match="Could not find"):
        find_task_definition(files, container="api")
    dup = {**files, "environments/demo/b.tf": REPLAY.replace('"web" {', '"web2" {', 1).replace("aws_ecs_service", "aws_x")}
    with pytest.raises(TaskEnvError, match="More than one"):
        find_task_definition(dup, container="web")
    with pytest.raises(TaskEnvError, match="must look like"):
        find_task_definition(files, container="web", tf_address="module.x.web")


def test_braces_and_brackets_inside_strings_do_not_confuse_the_scanner():
    text = REPLAY.replace('{ name = "PORT", value = "3000" }', '{ name = "PORT", value = "a ] } [ { ${local.x}" }')
    out = inject_env(text, resource="web", container="web", entries=KEY)
    assert env_names(out, resource="web", container="web")[-2:] == ["BACKUP_AWS_ACCESS_KEY_ID", "BACKUP_AWS_SECRET_ACCESS_KEY"]


@pytest.mark.skipif(shutil.which("terraform") is None, reason="terraform not installed")
def test_terraform_fmt_accepts_the_edited_file(tmp_path: Path):
    out = inject_env(inject_env(REPLAY, resource="web", container="web", entries=DB), resource="web", container="web", entries=KEY)
    (tmp_path / "replay.tf").write_text(out)
    res = subprocess.run(["terraform", "fmt", "-check", "-diff", str(tmp_path)], capture_output=True, text=True)
    assert res.returncode == 0, res.stdout + res.stderr
