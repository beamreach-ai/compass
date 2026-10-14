"""The open default language model: Claude through the Anthropic SDK.

A ``TextModel`` (compass_core.llm) on the user's own credentials. The SDK is
an optional extra (``pip install compass-open[anthropic]``); nothing here
imports it until a model is built, and ``install_if_available`` only installs
the factory when the SDK is importable, so an install without it keeps the
"no model" behaviour every caller already handles.

Settings, from the workspace config's ``anthropic`` block or the environment:

    anthropic.model     COMPASS_ANTHROPIC_MODEL   default claude-opus-5-5
    anthropic.effort    COMPASS_ANTHROPIC_EFFORT  low | medium | high | xhigh | max, default medium
    anthropic.fallbacks                           false to turn the refusal fallback off

Credentials resolve the way the SDK does (ANTHROPIC_API_KEY, ANTHROPIC_AUTH_TOKEN,
or an ``ant auth login`` profile).

The callers pass ``temperature`` because the product's Bedrock client takes
it; current Claude models reject sampling parameters, so it is not forwarded.
"""

from __future__ import annotations

import importlib.util
import logging
import os
from typing import Any, Dict, List, Optional

from compass_core import llm

logger = logging.getLogger("compass.anthropic")

DEFAULT_MODEL = "claude-opus-5-5"
DEFAULT_EFFORT = "medium"
FALLBACK_BETA = "server-side-fallback-2026-07-01"


def available() -> bool:
    """The SDK is installed and a credential is in reach: an API key or auth token in the
    environment, or an `ant auth login` profile on disk. Without one, the model-free path
    is the right behaviour, not a request that fails."""
    if importlib.util.find_spec("anthropic") is None:
        return False
    if os.getenv("ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_AUTH_TOKEN"):
        return True
    profile_dir = os.path.expanduser(os.getenv("ANTHROPIC_CONFIG_DIR") or "~/.config/anthropic")
    return os.path.isdir(profile_dir) and any(os.scandir(profile_dir))


def _settings(config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    block = (config or {}).get("anthropic") if isinstance((config or {}).get("anthropic"), dict) else {}
    return {
        "model": str(block.get("model") or os.getenv("COMPASS_ANTHROPIC_MODEL") or DEFAULT_MODEL),
        "effort": str(block.get("effort") or os.getenv("COMPASS_ANTHROPIC_EFFORT") or DEFAULT_EFFORT),
        "fallbacks": block.get("fallbacks", True) is not False,
    }


class AnthropicModel:
    def __init__(self, config: Optional[Dict[str, Any]] = None, *, client: Any = None) -> None:
        settings = _settings(config)
        self.model = settings["model"]
        self.effort = settings["effort"]
        self.fallbacks = settings["fallbacks"]
        if client is None:
            import anthropic  # the optional extra

            client = anthropic.Anthropic()
        self._client = client

    @property
    def name(self) -> str:
        return self.model

    def call(
        self,
        messages: List[Dict[str, Any]],
        max_tokens: int = 4096,
        temperature: float = 0.0,  # accepted for interface parity; never sent
        timeout: int = 60,
        system_prompt: Optional[str] = None,
    ) -> str:
        request: Dict[str, Any] = {
            "model": self.model,
            "max_tokens": int(max_tokens),
            "messages": messages,
            "output_config": {"effort": self.effort},
        }
        if system_prompt:
            request["system"] = system_prompt
        if self.fallbacks:
            # On a policy decline the API re-runs the request on a fallback model in the same call.
            request["betas"] = [FALLBACK_BETA]
            request["fallbacks"] = "default"

        response = self._client.with_options(timeout=float(timeout)).beta.messages.create(**request)

        if getattr(response, "stop_reason", None) == "refusal":
            details = getattr(response, "stop_details", None)
            category = getattr(details, "category", None) if details is not None else None
            raise RuntimeError(f"the model declined the request (category: {category or 'unspecified'})")
        if getattr(response, "stop_reason", None) == "max_tokens":
            logger.warning("model output hit max_tokens=%s; the result may be truncated", max_tokens)

        return "".join(block.text for block in response.content if getattr(block, "type", None) == "text")


def factory(config: Optional[Dict[str, Any]], target: Optional[str]) -> Optional[llm.TextModel]:
    if not available():
        return None
    return AnthropicModel(config)


def install_if_available() -> bool:
    """Make Claude the configured model when the SDK is installed. Returns whether it was."""
    if not available():
        logger.debug("anthropic SDK not installed; no language model configured")
        return False
    llm.set_model_factory(factory)
    return True
