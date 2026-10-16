"""Run one command of an open package from the shell.

    compass honeypot plant --workspace demo --payload token.json
    compass iac import --workspace demo repo=org/infra resource_ids='["arn:..."]'
    compass prs sync --workspace demo
    compass mapper run --workspace demo

The workspace config comes from ``compass_core.config`` (``COMPASS_CONFIG`` or
``config/<workspace>.json``). ``key=value`` arguments are parsed as JSON when
they are JSON, else kept as strings; ``--payload`` loads a JSON object first.
Follow-up jobs run in this process through the open dispatcher. With the
``anthropic`` extra installed, Claude is the model (compass_core.anthropic_model);
without it the commands run model-free. The result is printed as JSON.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from typing import Any, Dict, List, Optional

from compass_core import anthropic_model, captains, dispatch, llm

CORE_SHORTHAND = {"init_config", "init_tables", "init_alerts", "serve", "api", "login", "captains", "enable", "disable", "configure", "catalog", "install", "uninstall", "validate"}


def parse_pairs(pairs: List[str]) -> Dict[str, Any]:
    payload: Dict[str, Any] = {}
    for pair in pairs:
        if "=" not in pair:
            raise SystemExit(f"expected key=value, got {pair!r}")
        key, raw = pair.split("=", 1)
        try:
            payload[key] = json.loads(raw)
        except json.JSONDecodeError:
            payload[key] = raw
    return payload


def build_payload(args: argparse.Namespace) -> Dict[str, Any]:
    payload: Dict[str, Any] = {}
    if args.payload:
        with open(args.payload, encoding="utf-8") as handle:
            loaded = json.load(handle)
        if not isinstance(loaded, dict):
            raise SystemExit(f"{args.payload} must hold a JSON object")
        payload.update(loaded)
    payload.update(parse_pairs(args.pairs))
    if args.workspace:
        payload["workspace_id"] = args.workspace
    return payload


def load_commands(captain: str) -> None:
    """Import the captain's commands module, which registers its COMMANDS (idempotent).

    The captains come from the registry (compass_core.captains.discover)."""
    try:
        captains.load_commands(captain)
    except LookupError as exc:
        raise SystemExit(str(exc)) from None


def main(argv: Optional[List[str]] = None, *, captain: Optional[str] = None) -> int:
    from compass_core import hosts

    hosts.install()  # before the captain choices are read: a host may register captains
    argv = list(sys.argv[1:] if argv is None else argv)
    # `compass serve` / `compass init_tables` read better than `compass core serve`.
    if captain is None and argv and argv[0] in CORE_SHORTHAND:
        argv.insert(0, "core")
    parser = argparse.ArgumentParser(prog=f"python -m {captain}" if captain else "compass", description=__doc__.split("\n\n")[0])
    if captain is None:
        parser.add_argument("captain", choices=sorted(captains.commands_modules()))
    parser.add_argument("command")
    parser.add_argument("pairs", nargs="*", metavar="key=value")
    parser.add_argument("--workspace", "-w", default=None, help="workspace id (default: DEFAULT_WORKSPACE or TARGET)")
    parser.add_argument("--payload", "-p", default=None, help="JSON file merged into the payload")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)

    logging.basicConfig(level=getattr(logging, str(args.log_level).upper(), logging.INFO), format="%(name)s %(levelname)s %(message)s")
    chosen = captain or args.captain
    load_commands(chosen)
    dispatch.use_inline_queue()
    if llm.model_for() is None:
        anthropic_model.install_if_available()

    try:
        result = dispatch.dispatch(chosen, args.command, build_payload(args))
    except (LookupError, ValueError, RuntimeError, captains.CaptainUnavailable) as exc:  # unknown command, no config, a bad argument, a captain that is off
        print(str(exc.args[0] if exc.args else exc), file=sys.stderr)
        return 2
    json.dump(result, sys.stdout, indent=2, default=str)
    sys.stdout.write("\n")
    return 0
