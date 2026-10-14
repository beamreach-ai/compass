"""What the watcher tells the captain that opened a PR when that PR closes.

The watcher is captain-agnostic (docs/pr_watcher_spec.md). When a PR it
tracks is merged or declined, the originating captain may want to know: the
engine marks its finding resolved. That is the originating side's business,
so it registers a hook here per origin captain and the watcher calls it.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Optional

# hook(finding_id, status, *, config, workspace_id)
StatusHook = Callable[..., None]

_hooks: Dict[str, StatusHook] = {}


def register_status_hook(captain: str, hook: Optional[StatusHook]) -> None:
    if hook is None:
        _hooks.pop(captain, None)
    else:
        _hooks[captain] = hook


def status_hook(captain: Optional[str]) -> Optional[StatusHook]:
    return _hooks.get(str(captain or ""))


def registered() -> Dict[str, Any]:
    return dict(_hooks)
