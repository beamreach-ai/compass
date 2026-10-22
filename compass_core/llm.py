"""The language model the open tools call, when one is configured.

Three places in the open packages ask a model for text: the decoys' incident
summary and action plan, the drift-fix HCL in code-as-cloud, and the PR
watcher's comment classification and replies. Each already has a path for
"no model": a deterministic plan, a template reply, a clear error. So the open
default is no model at all, and the product installs its Bedrock client with
``set_model_factory``.

A ``TextModel`` has the one method every caller uses::

    model.call(messages, max_tokens=..., temperature=..., timeout=..., system_prompt=...) -> str
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Protocol


class TextModel(Protocol):
    def call(
        self,
        messages: List[Dict[str, Any]],
        max_tokens: int = ...,
        temperature: float = ...,
        timeout: int = ...,
        system_prompt: Optional[str] = None,
    ) -> str: ...


ModelFactory = Callable[[Optional[Dict[str, Any]], Optional[str]], Optional[TextModel]]

_factory: Optional[ModelFactory] = None


def set_model_factory(factory: Optional[ModelFactory]) -> None:
    """Install how a model is built for a workspace config; ``None`` means no model."""
    global _factory
    _factory = factory


def model_for(config: Optional[Dict[str, Any]] = None, *, target: Optional[str] = None) -> Optional[TextModel]:
    """The model for this config, or None when nothing is configured. Every call it makes
    claims one unit of the workspace's model budget first (``compass_core.model_budget``),
    unless the model counts its own calls (``budgeted = True``)."""
    if _factory is None:
        return None
    model = _factory(config, target)
    if model is None or getattr(model, "budgeted", False):
        return model
    return _Budgeted(model, config)


class _Budgeted:
    """A model whose every call is claimed from the workspace's budget before it is made."""

    budgeted = True

    def __init__(self, model: TextModel, config: Optional[Dict[str, Any]]) -> None:
        self._model = model
        self._config = config

    @property
    def name(self) -> str:
        return model_name(self._model)

    def _workspace_config(self) -> Dict[str, Any]:
        if isinstance(self._config, dict):
            return self._config
        # model_for() without a config (the decoys' summary): the default workspace's
        from compass_core.config import workspace_config
        from compass_core.workspace import default_workspace_id

        workspace_id = default_workspace_id()
        try:
            return dict(workspace_config(workspace_id), workspace_id=workspace_id) if workspace_id else {}
        except Exception:  # noqa: BLE001 - no config to count in; claim() decides what that means
            return {}

    def call(self, messages: List[Dict[str, Any]], *args: Any, **kwargs: Any) -> str:
        from compass_core import model_budget

        model_budget.claim(self._workspace_config())
        return self._model.call(messages, *args, **kwargs)

    def __getattr__(self, item: str) -> Any:
        return getattr(self._model, item)


def model_name(model: Optional[TextModel]) -> str:
    if model is None:
        return "none"
    name = getattr(model, "name", None)
    return str(name) if name else type(model).__name__
