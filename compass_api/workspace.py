"""Which workspace a request is for, and how its config is loaded and saved.

The open edition has one workspace whose config is a file
(``compass_core.config``). The product has many, in DynamoDB with their config
in Secrets Manager, and some of them belong to a signed-in user; it installs
its loader, saver and rules here with ``install``.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Optional, Tuple, Type

from compass_core.config import save_workspace_config as _save_file_config
from compass_core.config import workspace_config
from compass_core.workspace import Workspace

Loader = Callable[..., Any]          # (workspace_id, *, org_id, user_id) -> ctx with .workspace_id/.org_id/.user_id/.config
Saver = Callable[[Any, Dict[str, Any]], None]
UserWorkspace = Callable[[Optional[str]], bool]


def _open_loader(workspace_id: str, *, org_id: Optional[str] = None, user_id: Optional[str] = None, **_: Any) -> Workspace:
    # **_: force_refresh and friends — the file is read fresh every time anyway
    config = dict(workspace_config(workspace_id) or {})
    org = org_id or config.get("org_id") or config.get("org")
    return Workspace(workspace_id=workspace_id, org_id=str(org) if org else None, user_id=user_id, config=config)


def _open_saver(ctx: Any, config: Dict[str, Any]) -> None:
    _save_file_config(str(ctx.workspace_id), config)


_loader: Loader = _open_loader
_saver: Saver = _open_saver
_user_workspace: UserWorkspace = lambda _user_id: False  # noqa: E731
_lookup_errors: Tuple[Type[BaseException], ...] = (LookupError,)


def install(
    *,
    loader: Optional[Loader] = None,
    saver: Optional[Saver] = None,
    user_workspace: Optional[UserWorkspace] = None,
    lookup_errors: Optional[Tuple[Type[BaseException], ...]] = None,
) -> None:
    global _loader, _saver, _user_workspace, _lookup_errors
    if loader is not None:
        _loader = loader
    if saver is not None:
        _saver = saver
    if user_workspace is not None:
        _user_workspace = user_workspace
    if lookup_errors is not None:
        _lookup_errors = tuple(lookup_errors) + (LookupError,)


def load(workspace_id: str, *, org_id: Optional[str] = None, user_id: Optional[str] = None, **kwargs: Any) -> Any:
    return _loader(workspace_id, org_id=org_id, user_id=user_id, **kwargs)


def save(ctx: Any, config: Dict[str, Any]) -> None:
    _saver(ctx, config)


def uses_user_workspace(user_id: Optional[str]) -> bool:
    """True when a signed-in user's own id is their workspace (the product's personal workspaces)."""
    return bool(_user_workspace(user_id))


def lookup_errors() -> Tuple[Type[BaseException], ...]:
    return _lookup_errors
