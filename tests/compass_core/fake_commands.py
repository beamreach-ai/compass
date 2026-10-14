"""A command table for the CLI tests."""

from compass_core import dispatch

COMMANDS = {"echo": lambda payload: {"echo": payload}}

dispatch.register("fake", COMMANDS)
