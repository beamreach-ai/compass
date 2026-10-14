"""Delivery channels the open tools cannot provide on their own.

Webhooks, Slack and SMTP are in the open alert dispatcher. Managed email
(``beamreach_email`` endpoints) goes through a relay the product runs; the
open default has none and says so, and the product installs its client with
``set_email_relay``.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Protocol


class EmailRelay(Protocol):
    def send(
        self,
        *,
        license_key: Optional[str],
        install_id: Optional[str],
        to: List[str],
        subject: str,
        text: str,
        html: Optional[str] = None,
        api_url: Optional[str] = None,
    ) -> Dict[str, Any]: ...


class NoRelay:
    def send(self, **_: Any) -> Dict[str, Any]:
        raise RuntimeError(
            "managed email needs the Beamreach relay, which this install does not have; "
            "use an SMTP, webhook or Slack endpoint instead"
        )


_relay: EmailRelay = NoRelay()


def set_email_relay(relay: Optional[EmailRelay]) -> None:
    global _relay
    _relay = relay if relay is not None else NoRelay()


def email_relay() -> EmailRelay:
    return _relay
