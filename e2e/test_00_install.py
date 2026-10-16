"""4.1 Install and first run (docs/e2e.md): the quickstart, as written, on an empty directory."""

from __future__ import annotations

import httpx

from e2e import fixture


def test_ins_1_the_quickstart_brings_up_five_services_that_answer(stack):
    services = {line.split()[0]: line for line in stack.compose("ps", "--format", "{{.Service}} {{.State}}").stdout.splitlines()}
    for name in ("dynamodb", "compass", "api", "dashboard", "nginx"):
        assert name in services and "running" in services[name], services
    assert httpx.get(f"{stack.web}/health").json() == {"status": "ok"}
    assert httpx.get(f"{stack.web}/login").status_code == 200


def test_ins_2_init_config_prints_a_login_that_signs_in_and_a_wrong_password_does_not(stack):
    login = stack.records["init_config"]["login"]
    assert login["url"].endswith(str(stack.web.rsplit(":", 1)[1])) and login["username"] == "admin" and len(login["password"]) >= 12
    assert stack.records["init_config"]["github_token"] == "from GITHUB_TOKEN"
    assert stack.records["init_config"]["aws_account"] == fixture.ACCOUNT
    good = stack.anonymous().post("/api/auth/login", json={"username": "admin", "password": login["password"]})
    assert good.status_code == 200
    bad = stack.anonymous().post("/api/auth/login", json={"username": "admin", "password": "not-it"})
    assert bad.status_code == 401


def test_ins_3_init_config_does_not_overwrite_a_workspace_without_being_told(stack):
    error = stack.cli("init_config", "workspace=local", expect=2)
    assert "overwrite=true" in error and "Traceback" not in error


def test_ins_4_init_tables_creates_seven_tables_and_finds_them_the_second_time(stack):
    first, again = stack.records["init_tables"], stack.records["init_tables_again"]
    assert len(first["created"]) == 7 and first["present"] == []
    assert again["created"] == [] and sorted(again["present"]) == sorted(first["created"])


def test_ins_5_the_sign_in_in_env_wins_over_the_generated_one(stack):
    stack.env(APP_USERNAME="ops", APP_PASSWORD="e2e-env-password")
    try:
        stack.restart("api")
        assert stack.anonymous().post("/api/auth/login", json={"username": "ops", "password": "e2e-env-password"}).status_code == 200
        generated = stack.records["init_config"]["login"]["password"]
        assert stack.anonymous().post("/api/auth/login", json={"username": "admin", "password": generated}).status_code == 401
        assert stack.cli("login")["password"] == "the APP_PASSWORD in your .env"
    finally:
        stack.env(APP_USERNAME="", APP_PASSWORD="")
        stack.restart("api")


def test_ins_6_without_any_password_compass_warns_that_sign_in_is_off(stack):
    """The login block for a config with no password and no APP_PASSWORD (a hand-written config)."""
    printed = stack.compose("run", "--rm", "-T", "--entrypoint", "python", "compass", "-c",
                            "from compass_core.commands import dashboard_login; import json; print(json.dumps(dashboard_login({})))")
    assert "anyone who can reach this port can use Compass" in printed.stdout, printed.stderr


def test_ins_7_mistakes_are_one_line_and_exit_2(stack):
    unknown = stack.cli("core", "no_such_command", expect=2)
    assert "unknown command core.no_such_command" in unknown and "Traceback" not in unknown
    no_captain = stack.cli("configure", "captain=nobody", expect=2)
    assert "nobody" in no_captain and "Traceback" not in no_captain
    no_config = stack.cli("mapper", "run", "--workspace", "does-not-exist", expect=2)
    assert "no configuration for workspace 'does-not-exist'" in no_config and "Traceback" not in no_config


def test_ins_8_migrate_runs_on_start_and_never_runs_a_step_twice(stack):
    first = stack.cli("migrate")
    assert first["created"] == [] and len(first["present"]) == 7 and first["migrations"] == []
    logs = stack.compose("logs", "--no-color", "api").stdout
    assert "migrate did not run" not in logs
