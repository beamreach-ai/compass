"""Polls the cross-account alert queue and records honeytoken trips.

An EventBridge rule in each target account delivers directly to this queue in
the control-plane account. The queue's policy names the source rules, so only
those can put messages on it — no public endpoint, no shared secret.

Runs as a long-lived loop beside the honeypot Celery worker rather than as a
task, because long polling is the point: a honeytoken trip should surface in
seconds, and a scheduled task would trade that for either latency or wasted
polls.
"""

from __future__ import annotations

import json
import logging
import os
import re
import signal
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import boto3

from captains.honeypot.ingestion import record_alert
from compass_core.tables import tables

logger = logging.getLogger("honeypot-alert-consumer")

# Long poll for the maximum SQS allows: fewer empty receives, and a message
# that arrives mid-wait is returned immediately rather than after a sleep.
WAIT_TIME_SECONDS = 20
MAX_MESSAGES = 10
# Long enough to record the alert and enqueue notification, short enough that a
# crashed consumer's messages come back quickly.
VISIBILITY_TIMEOUT = 60



def sent_timestamp_to_iso(value: Any) -> Optional[str]:
    """SQS `SentTimestamp` (epoch milliseconds) as an ISO-8601 UTC string."""
    try:
        return datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _region_from_queue_url(queue_url: str) -> Optional[str]:
    """Region an SQS queue lives in, taken from its own URL.

    Deriving it rather than configuring it keeps the two regions from being
    conflated by a single AWS_REGION, and means adding a queue in another
    region needs no extra setting.
    """
    match = re.match(r"https://sqs\.([a-z0-9-]+)\.amazonaws\.com/", queue_url or "")
    return match.group(1) if match else None


class AlertConsumer:
    def __init__(
        self,
        queue_url: Optional[str] = None,
        region: Optional[str] = None,
        sqs_client: Any = None,
    ):
        self.queue_url = queue_url or os.getenv("HONEYPOT_ALERT_QUEUE_URL", "")

        # Two regions, deliberately distinct.
        #
        # The queue lives wherever the honeytokens do, because EventBridge cannot
        # deliver to SQS across regions. The workspace tables live in the
        # control plane's own region. Treating these as one value silently
        # sends DynamoDB lookups to the honeytoken's region, where the tables do
        # not exist — the alert is received and then cannot be recorded.
        self.region = region or os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION")
        self.queue_region = _region_from_queue_url(self.queue_url) or self.region

        self._sqs = sqs_client or (
            boto3.client("sqs", region_name=self.queue_region) if self.queue_url else None
        )
        self._stop = False

    def stop(self, *_args) -> None:
        logger.info("alert consumer stopping after current batch")
        self._stop = True

    # ------------------------------------------------------------------ body

    def handle_message(self, body: Any, received_at: Optional[str] = None, message_id: Optional[str] = None) -> bool:
        """Record one alert. Returns True when the message may be deleted.

        A malformed message is NOT deleted: it is left for the queue's redrive
        policy (maxReceiveCount 3) to move to the dead-letter queue, where an
        operator can see it. Dropping it would silently lose what may be a real
        intrusion signal with a broken template. Returning False leaves every
        message un-deleted, so the same path serves transient failures.

        `received_at` is the queue's SentTimestamp as ISO-8601 (when the event
        reached the queue), stored apart from the event's own time.
        """
        if not isinstance(body, dict):
            logger.warning("Alert message %s is not a JSON object; leaving it for the dead-letter queue", message_id)
            return False
        workspace_id = str(body.get("workspace_id") or "")
        # Plants deployed before the rename still send `asset_id`.
        honeytoken_id = str(body.get("honeytoken_id") or body.get("token_id") or body.get("asset_id") or "")
        if not workspace_id or not honeytoken_id:
            logger.warning(
                "Alert message %s has no workspace_id/honeytoken_id; leaving it for the dead-letter queue: %s",
                message_id, json.dumps(body, default=str)[:200],
            )
            return False

        from compass_core.config import workspace_config
        from compass_core.keys import workspace_pk

        config = workspace_config(workspace_id)
        dynamodb_config = config.get("dynamodb_config") or {}
        table_name = tables(dynamodb_config).items
        if not table_name:
            logger.error("Workspace %s has no table configured; leaving message for retry", workspace_id)
            return False

        table = boto3.resource("dynamodb", region_name=self.region).Table(table_name)

        from captains.honeypot.placement import get_placement, placement_summary, to_native
        from compass_core.queue import job_queue

        record_alert(
            table,
            workspace_pk(workspace_id),
            {**body, "honeytoken_id": honeytoken_id},
            workspace_id=workspace_id,
            received_at=received_at,
            queue_client=job_queue(),
            placement_summary=placement_summary,
            get_placement=get_placement,
            to_native=to_native,
        )
        return True

    def poll_once(self) -> int:
        resp = self._sqs.receive_message(
            QueueUrl=self.queue_url,
            MaxNumberOfMessages=MAX_MESSAGES,
            WaitTimeSeconds=WAIT_TIME_SECONDS,
            VisibilityTimeout=VISIBILITY_TIMEOUT,
            MessageAttributeNames=["All"],
            AttributeNames=["SentTimestamp"],
        )
        messages: List[Dict[str, Any]] = resp.get("Messages") or []
        handled = 0

        for message in messages:
            try:
                body = json.loads(message.get("Body") or "{}")
            except ValueError:
                logger.warning(
                    "Alert message %s is not JSON; leaving it for the dead-letter queue", message.get("MessageId")
                )
                continue

            try:
                received_at = sent_timestamp_to_iso((message.get("Attributes") or {}).get("SentTimestamp"))
                if self.handle_message(body, received_at=received_at, message_id=message.get("MessageId")):
                    self._delete(message)
                    handled += 1
            except Exception:
                # Left un-deleted on purpose: SQS redelivers, and persistent
                # failures land in the dead-letter queue where they can be seen
                # rather than silently dropped.
                logger.exception("Failed to handle alert message %s", message.get("MessageId"))

        return handled

    def _delete(self, message: Dict[str, Any]) -> None:
        try:
            self._sqs.delete_message(QueueUrl=self.queue_url, ReceiptHandle=message["ReceiptHandle"])
        except Exception:
            logger.exception("Failed to delete message %s", message.get("MessageId"))

    def run(self, install_signals: bool = True) -> None:
        if not self.queue_url:
            logger.warning("HONEYPOT_ALERT_QUEUE_URL is not set; alert consumer will not start")
            return

        # Signal handlers can only be installed from the main thread; when
        # several consumers run in threads, main() installs one handler that
        # stops them all.
        if install_signals:
            signal.signal(signal.SIGTERM, self.stop)
            signal.signal(signal.SIGINT, self.stop)
        logger.info("alert consumer polling %s (data region %s)", self.queue_url, self.region)

        while not self._stop:
            try:
                self.poll_once()
            except Exception:
                logger.exception("alert consumer poll failed; backing off")
                time.sleep(5)


def queue_urls_from_env(environ: Optional[Dict[str, str]] = None) -> List[str]:
    """One queue per watched region: HONEYPOT_ALERT_QUEUE_URLS (comma-separated)
    wins; the single HONEYPOT_ALERT_QUEUE_URL is kept for older deployments."""
    env = os.environ if environ is None else environ
    raw = env.get("HONEYPOT_ALERT_QUEUE_URLS") or env.get("HONEYPOT_ALERT_QUEUE_URL") or ""
    seen: List[str] = []
    for url in (u.strip() for u in raw.split(",")):
        if url and url not in seen:
            seen.append(url)
    return seen


def run_all(queue_urls: List[str]) -> None:
    """Poll every queue concurrently; a trip in one region must not wait on
    another region's long poll."""
    consumers = [AlertConsumer(queue_url=url) for url in queue_urls]

    def _stop_all(*_args) -> None:
        for consumer in consumers:
            consumer.stop()

    signal.signal(signal.SIGTERM, _stop_all)
    signal.signal(signal.SIGINT, _stop_all)

    threads = [
        threading.Thread(target=consumer.run, kwargs={"install_signals": False}, name=f"alerts-{i}", daemon=True)
        for i, consumer in enumerate(consumers)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        while thread.is_alive():
            thread.join(timeout=1)


def main() -> None:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper())
    urls = queue_urls_from_env()
    if not urls:
        logger.warning("HONEYPOT_ALERT_QUEUE_URLS is not set; alert consumer will not start")
        return
    if len(urls) == 1:
        AlertConsumer(queue_url=urls[0]).run()
    else:
        run_all(urls)


if __name__ == "__main__":
    main()


def queue_urls(config: Dict[str, Any]) -> List[str]:
    """The alert queues a workspace consumes: ``honeypot.alert_queue_urls`` or
    ``honeypot.alert_queue_url``, else ``HONEYPOT_ALERT_QUEUE_URL(S)``."""
    hp = config.get("honeypot") if isinstance(config.get("honeypot"), dict) else {}
    urls = hp.get("alert_queue_urls") or ([hp["alert_queue_url"]] if hp.get("alert_queue_url") else [])
    if not urls:
        raw = os.getenv("HONEYPOT_ALERT_QUEUE_URLS") or os.getenv("HONEYPOT_ALERT_QUEUE_URL") or ""
        urls = [u.strip() for u in raw.split(",") if u.strip()]
    return [str(u) for u in urls]


def run_service(workspace_id: str, config: Dict[str, Any], stop: threading.Event, *, interval: Optional[int] = None) -> None:
    """Consume every configured alert queue until ``stop`` is set
    (a `compass serve` service, compass_core.captains.ServiceSpec)."""
    urls = queue_urls(config)
    if not urls:
        logger.warning("no decoy alert queue configured (honeypot.alert_queue_url); alerts will not be consumed")
        return
    consumers = []
    threads = []
    for url in urls:
        consumer = AlertConsumer(queue_url=url)
        thread = threading.Thread(target=consumer.run, kwargs={"install_signals": False}, name=f"alerts:{url.rsplit('/', 1)[-1]}", daemon=True)
        thread.start()
        consumers.append(consumer)
        threads.append(thread)
        logger.info("consuming decoy alerts from %s", url)
    stop.wait()
    for consumer in consumers:
        consumer.stop()
    for thread in threads:
        thread.join(timeout=5)
