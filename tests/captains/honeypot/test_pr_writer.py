"""PR writer against a recording fake GitHub client."""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from github import GithubException

from captains.honeypot import pr_writer
from captains.honeypot.pr_writer import HoneypotPRWriter, parse_honeytokens_tf
from captains.honeypot.terraform_generator import generate_terraform, module_slug, root_outputs

QUEUE = "arn:aws:sqs:us-east-1:111111111111:compass-alerts"


class FakeContent:
    def __init__(self, path, text, sha, type_="file"):
        self.path, self.sha, self.type = path, sha, type_
        self.decoded_content = text.encode()


class FakeRepo:
    def __init__(self, files=None):
        self.files = dict(files or {})  # path -> text (the single branch under test)
        self.calls = []
        self.prs = []
        self._n = 0

    def get_branch(self, name):
        return SimpleNamespace(commit=SimpleNamespace(sha="base"))

    def create_git_ref(self, ref, sha):
        self.calls.append(("ref", ref))

    def get_contents(self, path, ref=None):
        if path in self.files:
            self._n += 1
            return FakeContent(path, self.files[path], f"sha{self._n}")
        children = [p for p in self.files if p.startswith(path.rstrip("/") + "/")]
        if children:
            return [FakeContent(p, self.files[p], "d") for p in children]
        raise GithubException(404, {"message": "Not Found"}, {})

    def create_file(self, path, msg, content, branch=None):
        assert path not in self.files
        self.files[path] = content
        self.calls.append(("create", path))

    def update_file(self, path, msg, content, sha, branch=None):
        assert path in self.files
        self.files[path] = content
        self.calls.append(("update", path))

    def delete_file(self, path, msg, sha, branch=None):
        del self.files[path]
        self.calls.append(("delete", path))

    def create_pull(self, title, body, head, base):
        self.prs.append({"title": title, "body": body, "head": head})
        return SimpleNamespace(html_url="https://github.com/o/r/pull/1")


@pytest.fixture
def make_writer(monkeypatch):
    monkeypatch.setattr(pr_writer, "resolve_github_token", lambda *a, **k: "t")

    def _make(repo):
        monkeypatch.setattr(pr_writer, "github_client", lambda token: SimpleNamespace(get_repo=lambda name: repo))
        return HoneypotPRWriter({"github": {"honeypot_repo": "o/r"}})

    return _make


def _token(name="svc_backup", cls="aws_credential", hid="ht-1"):
    return {"honeytoken_id": hid, "honeytoken_class": cls, "name": name, "workspace_id": "ws-1"}


def _plant(writer, token, module_path=None):
    return writer.create_plant_pr(
        honeytoken=token,
        hcl_files=generate_terraform(token),
        resolved_module_path=module_path,
        module_slug=module_slug(token),
        queue_arn=QUEUE,
        workspace_id="ws-1",
        root_outputs=root_outputs(token),
    )


def _decommission(writer, token, module_path=None):
    return writer.create_decommission_pr(
        honeytoken=token, module_slug=module_slug(token), resolved_module_path=module_path
    )


def test_first_plant_creates_account_module_and_token_block(make_writer):
    repo = FakeRepo({"main.tf": "# customer file\n"})
    _plant(make_writer(repo), _token())

    doc = parse_honeytokens_tf(repo.files["services.tf"])
    assert set(doc.modules) == {"svc_account", "svc_svc_backup"}
    assert doc.modules["svc_account"]["alert_queue_arn"] == f'"{QUEUE}"'
    token_mod = doc.modules["svc_svc_backup"]
    assert token_mod["source"] == '"./services/svc_backup"'
    assert token_mod["workspace_id"] == '"ws-1"'
    assert token_mod["alert_delivery_role_arn"] == "module.svc_account.alert_delivery_role_arn"
    assert doc.outputs == {"svc_svc_backup_access_key_id": "module.svc_svc_backup.access_key_id"}
    assert "secret" not in repo.files["services.tf"]

    assert "services/_account/main.tf" in repo.files
    assert "services/svc_backup/main.tf" in repo.files
    assert "services/svc_backup/variables.tf" in repo.files
    assert repo.files["main.tf"] == "# customer file\n"  # customer files untouched
    assert len(repo.prs) == 1


def test_second_token_reuses_account_module(make_writer):
    repo = FakeRepo()
    w = make_writer(repo)
    _plant(w, _token())
    account_before = repo.files["services/_account/main.tf"]
    repo.calls.clear()
    _plant(w, _token("billing_gw", "api_token", "ht-2"))

    assert ("create", "services/_account/main.tf") not in repo.calls
    assert repo.files["services/_account/main.tf"] == account_before
    doc = parse_honeytokens_tf(repo.files["services.tf"])
    assert set(doc.modules) == {"svc_account", "svc_svc_backup", "svc_billing_gw"}
    assert "svc_billing_gw_callback_url" in doc.outputs


def test_replant_updates_in_place(make_writer):
    repo = FakeRepo()
    w = make_writer(repo)
    _plant(w, _token())
    first = repo.files["services.tf"]
    _plant(w, _token())
    assert repo.files["services.tf"] == first
    assert repo.files["services.tf"].count('module "svc_svc_backup"') == 1
    assert repo.files["services.tf"].count('module "svc_account"') == 1

    # A changed workspace updates the existing block instead of adding another.
    w.create_plant_pr(
        honeytoken=_token(), hcl_files=generate_terraform(_token()), module_slug="svc_backup",
        queue_arn=QUEUE, workspace_id="ws-2", root_outputs={"access_key_id": "access_key_id"},
    )
    text = repo.files["services.tf"]
    assert text.count('module "svc_svc_backup"') == 1 and '"ws-2"' in text and '"ws-1"' not in text


def test_module_path_prefix(make_writer):
    repo = FakeRepo()
    _plant(make_writer(repo), _token(), module_path="infra/prod/")
    assert "infra/prod/services.tf" in repo.files
    assert "infra/prod/services/_account/main.tf" in repo.files
    assert "infra/prod/services/svc_backup/main.tf" in repo.files
    assert parse_honeytokens_tf(repo.files["infra/prod/services.tf"]).has_account_module


def test_decommission_removes_block_and_directory_keeps_account(make_writer):
    repo = FakeRepo()
    w = make_writer(repo)
    _plant(w, _token())
    _plant(w, _token("billing_gw", "api_token", "ht-2"))

    _decommission(w, _token())

    doc = parse_honeytokens_tf(repo.files["services.tf"])
    assert set(doc.modules) == {"svc_account", "svc_billing_gw"}
    assert not any(o.startswith("svc_svc_backup") for o in doc.outputs)
    assert "svc_billing_gw_callback_url" in doc.outputs
    assert not any(p.startswith("services/svc_backup/") for p in repo.files)
    assert "services/billing_gw/main.tf" in repo.files
    assert "services/_account/main.tf" in repo.files

    # Removing the last token still keeps svc_account.
    _decommission(w, _token("billing_gw", "api_token", "ht-2"))
    doc = parse_honeytokens_tf(repo.files["services.tf"])
    assert set(doc.modules) == {"svc_account"} and not doc.outputs
    assert "services/_account/variables.tf" in repo.files


def test_plant_requires_queue_arn(make_writer):
    w = make_writer(FakeRepo())
    with pytest.raises(RuntimeError, match="queue ARN"):
        w.create_plant_pr(
            honeytoken=_token(), hcl_files={}, module_slug="x", queue_arn="", workspace_id="ws"
        )


def test_unmanaged_blocks_in_honeytokens_tf_are_refused():
    with pytest.raises(ValueError):
        parse_honeytokens_tf('resource "aws_s3_bucket" "b" {}\n')


def test_module_pr_adds_responder_block_and_keeps_tokens(make_writer):
    from captains.honeypot.pr_writer import parse_honeytokens_tf
    from captains.honeypot.responder_role import MODULE_DIR, MODULE_NAME, generate_responder_module

    repo = FakeRepo()
    writer = make_writer(repo)
    _plant(writer, _token())  # an existing token block must survive
    module = generate_responder_module(
        [{"action_type": "ecs_restart_task", "scope": {"service_arns": ["arn:aws:ecs:us-east-1:1:service/web/web"]}, "enabled": True}],
        account_id="1", region="us-east-1", trust_principal_arns=["arn:aws:iam::2:role/worker"], external_id="x",
    )
    url = writer.create_module_pr(
        module_name=MODULE_NAME, module_dir=MODULE_DIR, files=module["files"], outputs={"responder_role_arn": "role_arn"},
        branch_suffix="responder-role", title="Operations responder role", body="b",
    )
    assert url
    assert "services/_responder/main.tf" in repo.files
    doc = parse_honeytokens_tf(repo.files["services.tf"])
    assert MODULE_NAME in doc.modules and doc.modules[MODULE_NAME]["source"] == '"./services/_responder"'
    assert any(n.startswith("svc_") and n not in (MODULE_NAME, "svc_account") for n in doc.modules), "token block kept"
    assert doc.outputs[f"{MODULE_NAME}_role_arn"] == f"module.{MODULE_NAME}.responder_role_arn"
    # re-running updates in place rather than duplicating
    writer.create_module_pr(
        module_name=MODULE_NAME, module_dir=MODULE_DIR, files=module["files"], outputs={"responder_role_arn": "role_arn"},
        branch_suffix="responder-role", title="Operations responder role", body="b",
    )
    assert repo.files["services.tf"].count(f'module "{MODULE_NAME}"') == 1


def test_list_inputs_survive_a_reparse(make_writer):
    """A second plant re-reads services.tf; list inputs (subnet_ids) must come
    back as HCL lists, not Python reprs (seen on the first real re-plant)."""
    from captains.honeypot.pr_writer import parse_honeytokens_tf, render_honeytokens_tf

    doc = parse_honeytokens_tf("")
    doc.upsert_account("arn:aws:sqs:us-east-1:1:q")
    doc.upsert_token("svc_db", queue_arn="arn:aws:sqs:us-east-1:1:q", workspace_id="ws", outputs={},
                     extra_inputs={"subnet_ids": ["subnet-1", "subnet-2"], "vpc_id": "vpc-1", "cpu": 256, "enabled": True})
    text = render_honeytokens_tf(doc)
    again = render_honeytokens_tf(parse_honeytokens_tf(text))
    assert again == text
    assert 'subnet_ids              = ["subnet-1", "subnet-2"]' in again or '["subnet-1", "subnet-2"]' in again
    assert "'" not in again


# ── HP-05: ecs_task_env placement edits the customer's task definition ──

from captains.honeypot.captain import HoneypotCaptain  # noqa: E402
from captains.honeypot.task_env import env_names  # noqa: E402
from tests.captains.honeypot.test_task_env import REPLAY  # noqa: E402

ROOT = "environments/demo"


def _ecs_token(cls, name, hid):
    return {**_token(name=name, cls=cls, hid=hid), "placement": {"type": "ecs_task_env", "container": "web"}}


def _plant_ecs(writer, token):
    writer.create_plant_pr(
        honeytoken=token,
        hcl_files=generate_terraform(token),
        resolved_module_path=ROOT,
        module_slug=module_slug(token),
        queue_arn=QUEUE,
        workspace_id="ws-1",
        root_outputs=root_outputs(token),
        task_env=HoneypotCaptain._task_env(token),
    )
    return writer.task_env_result


def test_plant_puts_the_variables_into_the_task_definition_and_decommission_takes_them_out(make_writer):
    repo = FakeRepo({f"{ROOT}/replay.tf": REPLAY, f"{ROOT}/main.tf": 'locals {\n  env = "x"\n}\n', f"{ROOT}/modules/other/main.tf": REPLAY})
    db = _ecs_token("db_credential", "svc-backup-reporting", "ht-db")
    key = _ecs_token("aws_credential", "ci-artifacts-reader", "ht-key")
    w = make_writer(repo)

    got = _plant_ecs(w, db)
    assert got == {"tf_address": "aws_ecs_task_definition.web", "file": f"{ROOT}/replay.tf", "env_vars": ["DB_HOST", "DB_PORT", "DB_USER", "DB_PASSWORD"]}
    slug = module_slug(db)
    assert f'{{ name = "DB_PASSWORD", value = module.svc_{slug}.password }}' in repo.files[f"{ROOT}/replay.tf"]
    assert repo.files[f"{ROOT}/modules/other/main.tf"] == REPLAY, "only root-level files are candidates"

    _plant_ecs(w, key)
    names = env_names(repo.files[f"{ROOT}/replay.tf"], resource="web", container="web")
    assert names[-6:] == ["DB_HOST", "DB_PORT", "DB_USER", "DB_PASSWORD", "BACKUP_AWS_ACCESS_KEY_ID", "BACKUP_AWS_SECRET_ACCESS_KEY"]
    # The repository holds references only: no generated secret material is written.
    assert "AKIA" not in repo.files[f"{ROOT}/replay.tf"]

    placed_db = {**db, "placement": {**db["placement"], **got}}
    w.create_decommission_pr(honeytoken=placed_db, module_slug=slug, resolved_module_path=ROOT, task_env=HoneypotCaptain._task_env(placed_db, remove=True))
    names = env_names(repo.files[f"{ROOT}/replay.tf"], resource="web", container="web")
    assert "DB_HOST" not in names and names[-2:] == ["BACKUP_AWS_ACCESS_KEY_ID", "BACKUP_AWS_SECRET_ACCESS_KEY"]


def test_other_placements_leave_customer_files_alone(make_writer):
    FakeRepo({f"{ROOT}/replay.tf": REPLAY})
    token = {**_token(cls="db_credential", name="svc-backup-reporting", hid="ht-x"), "placement": {"type": "manual"}}
    assert HoneypotCaptain._task_env(token) is None
    assert HoneypotCaptain._task_env({**_ecs_token("s3_object", "x", "ht-s3")}) is None
    with pytest.raises(RuntimeError, match="container"):
        HoneypotCaptain._task_env({**token, "placement": {"type": "ecs_task_env"}})
