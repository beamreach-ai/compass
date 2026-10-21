"""Loading the honeypot's commands registers its response actions: a process that runs the
grant verification or the self-audit without the alert consumer (a ticker) still knows
`ecs_restart_task` and `iam_deactivate_key`."""

from __future__ import annotations

import ast
import pathlib


def test_the_responses_are_registered_once_the_commands_load():
    from compass_core import actions, captains

    captains.load_commands("honeypot")
    assert {"ecs_restart_task", "iam_deactivate_key"} <= set(actions.registered("honeypot"))


def test_the_commands_module_imports_the_actions_itself():
    # not left to the alert consumer: the ticker and the CLI load only the commands
    source = pathlib.Path("captains/honeypot/commands.py").read_text()
    imported = {
        alias.name
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.ImportFrom) and node.module == "captains.honeypot"
        for alias in node.names
    }
    assert "actions" in imported
