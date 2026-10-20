"""What an error may say to a caller outside the process.

An exception's text can carry a stack trace, a path, an ARN or a key fragment. Results
that reach an API caller or the dashboard carry ``public(exc)`` instead: the exception's
class and, for an AWS error, its error code ("ClientError: AccessDenied"). The full error
goes to the log, where an operator reads it.
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger("compass.errors")


def public(exc: BaseException, *, context: Optional[str] = None) -> str:
    """A short, safe description of ``exc``; logs the full error once."""
    if context:
        logger.warning("%s: %s", context, exc, exc_info=exc)
    else:
        logger.warning("%s", exc, exc_info=exc)
    name = type(exc).__name__
    code = None
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        code = (response.get("Error") or {}).get("Code")
    if isinstance(code, str) and code.replace(".", "").isalnum():
        return f"{name}: {code}"
    return name
