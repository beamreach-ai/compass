import json

import pytest

from compass_core import captains, cli, dispatch, queue
from compass_core.captains import CaptainManifest


@pytest.fixture
def fake_captain(monkeypatch):
    monkeypatch.setattr(dispatch, "_registry", {})
    captains.register(CaptainManifest(name="fake", label="Fake", summary="a test captain", tier="open", feature="fake", commands="tests.compass_core.fake_commands"))
    previous = queue._factory
    yield
    captains.unregister("fake")
    queue.set_job_queue(previous)


def test_pairs_are_json_when_they_parse_and_strings_otherwise():
    assert cli.parse_pairs(["n=1", "ids=[\"a\",\"b\"]", "repo=org/infra", "flag=true"]) == {
        "n": 1,
        "ids": ["a", "b"],
        "repo": "org/infra",
        "flag": True,
    }


def test_runs_a_command_and_prints_json(fake_captain, capsys, tmp_path):
    payload = tmp_path / "p.json"
    payload.write_text(json.dumps({"from_file": 1, "n": 0}))
    rc = cli.main(["fake", "echo", "n=2", "--workspace", "ws-1", "--payload", str(payload)])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out == {"echo": {"from_file": 1, "n": 2, "workspace_id": "ws-1"}}


def test_captain_bound_entry_point_omits_the_captain_argument(fake_captain, capsys):
    assert cli.main(["echo", "a=1"], captain="fake") == 0
    assert json.loads(capsys.readouterr().out) == {"echo": {"a": 1}}


def test_unknown_command_is_an_error_not_a_traceback(fake_captain, capsys):
    assert cli.main(["fake", "nope"]) == 2
    assert "unknown command fake.nope" in capsys.readouterr().err


def test_unknown_captain_exits_with_the_known_ones():
    with pytest.raises(SystemExit, match="unknown captain 'zzz'; known: .*honeypot"):
        cli.load_commands("zzz")


def test_missing_workspace_config_is_a_message_not_a_traceback(fake_captain, capsys, monkeypatch):
    from compass_core import config

    monkeypatch.setattr(config, "_loader", None)
    monkeypatch.delenv("COMPASS_CONFIG", raising=False)
    dispatch.register("fake", {"needs_ws": lambda p: {"ws": config.workspace_config(p["workspace_id"])}})
    assert cli.main(["fake", "needs_ws", "--workspace", "does-not-exist"]) == 2
    assert "no configuration for workspace 'does-not-exist'" in capsys.readouterr().err


def test_the_core_commands_are_shorthand_and_update_takes_a_bare_verb():
    from compass_core import commands

    assert set(commands.COMMANDS) == cli.CORE_SHORTHAND
    assert {"migrate", "update", "connect"} <= cli.CORE_SHORTHAND
    assert cli.VERB_COMMANDS == {"update": ("check", "apply", "rollback"), "connect": ("template", "verify", "rotate")}


def test_a_config_that_exists_is_a_message_not_a_traceback(capsys, tmp_path):
    out = tmp_path / "local.json"
    out.write_text("{}")
    assert cli.main(["init_config", "workspace=local", f"out={out}"]) == 2
    err = capsys.readouterr().err
    assert "pass overwrite=true" in err and "Traceback" not in err


def test_a_workspace_that_is_not_the_config_file_s_is_not_quietly_the_config_file_s(tmp_path, monkeypatch):
    from compass_core import config

    path = tmp_path / "local.json"
    path.write_text('{"workspace_id": "local", "aws": {}}')
    monkeypatch.setenv("COMPASS_CONFIG", str(path))
    monkeypatch.setattr(config, "_loader", None)
    assert config.workspace_config("local")["workspace_id"] == "local"
    import pytest

    with pytest.raises(LookupError, match="no configuration for workspace 'loacl'"):
        config.workspace_config("loacl")
