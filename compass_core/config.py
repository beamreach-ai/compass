"""A workspace's configuration, by id.

Jobs carry a workspace id and the captain gets a resolved config. The one
open-package component that starts from an id alone is the decoys' alert
consumer (an SQS message names the workspace). The open default reads a JSON
file: ``COMPASS_CONFIG`` when set, else ``config/<workspace_id>.json``. The
product installs its multi-tenant loader (DynamoDB workspace row plus Secrets
Manager) with ``set_workspace_config_loader``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Callable, Dict, Optional

ConfigLoader = Callable[[str], Dict[str, Any]]

_loader: Optional[ConfigLoader] = None


def set_workspace_config_loader(loader: Optional[ConfigLoader]) -> None:
    global _loader
    _loader = loader


def _file_loader(workspace_id: str) -> Dict[str, Any]:
    explicit = os.getenv("COMPASS_CONFIG")
    candidates = [Path(explicit).expanduser()] if explicit else []
    candidates.append(Path("config") / f"{workspace_id}.json")
    for path in candidates:
        if path.is_file():
            with path.open(encoding="utf-8") as handle:
                data = json.load(handle)
            if not isinstance(data, dict):
                raise ValueError(f"{path} does not hold a JSON object")
            data.setdefault("workspace_id", workspace_id)
            return data
    raise LookupError(
        f"no configuration for workspace {workspace_id!r}: set COMPASS_CONFIG or create config/{workspace_id}.json"
    )


def workspace_config(workspace_id: str) -> Dict[str, Any]:
    return (_loader or _file_loader)(workspace_id)


ConfigSaver = Callable[[str, Dict[str, Any]], None]

_saver: Optional[ConfigSaver] = None


def set_workspace_config_saver(saver: Optional[ConfigSaver]) -> None:
    global _saver
    _saver = saver


def _file_saver(workspace_id: str, config: Dict[str, Any]) -> None:
    explicit = os.getenv("COMPASS_CONFIG")
    path = Path(explicit).expanduser() if explicit else Path("config") / f"{workspace_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(config, indent=2, default=str) + "\n", encoding="utf-8")
    try:
        tmp.chmod(0o600)
    except OSError:
        pass
    tmp.replace(path)


def save_workspace_config(workspace_id: str, config: Dict[str, Any]) -> None:
    """Write a workspace's config: the file the default loader reads, unless a saver is installed."""
    (_saver or _file_saver)(workspace_id, config)


def config_path(workspace_id: str) -> Optional[Path]:
    """The file the default loader would read for this workspace, if it exists (None under a custom loader)."""
    if _loader is not None:
        return None
    explicit = os.getenv("COMPASS_CONFIG")
    for path in ([Path(explicit)] if explicit else []) + [Path("config") / f"{workspace_id}.json"]:
        if path.is_file():
            return path
    return None
