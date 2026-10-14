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
    """The model for this config, or None when nothing is configured."""
    if _factory is None:
        return None
    return _factory(config, target)


def model_name(model: Optional[TextModel]) -> str:
    if model is None:
        return "none"
    name = getattr(model, "name", None)
    return str(name) if name else type(model).__name__
