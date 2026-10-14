"""Minimal Postgres v3 listener. Rejects every login, never serves or proxies data.

Every connection yields one event on the alert queue:
  trip  (critical): a StartupMessage followed by a password attempt
  probe (low):      no valid StartupMessage within STARTUP_TIMEOUT (port scan, garbage)
Stdlib + boto3 only.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import struct
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional

log = logging.getLogger("listener")

SSL_REQUEST, GSS_REQUEST, PROTOCOL_V3 = 80877103, 80877104, 196608
STARTUP_TIMEOUT = 5.0
MAX_STARTUP = 10_000
RATE_WINDOW = 2.0


class AlertSink:
    """Sends one JSON event to SQS. Tests inject a fake with the same `send`."""

    def __init__(self, queue_url: str, client: Any = None):
        self.queue_url = queue_url
        self._client = client

    def send(self, event: Dict[str, Any]) -> None:
        if self._client is None:
            import boto3

            self._client = boto3.client("sqs", region_name=self.queue_url.split(".")[1] if self.queue_url.count(".") > 2 else None)
        self._client.send_message(QueueUrl=self.queue_url, MessageBody=json.dumps(event))


def task_identity() -> str:
    base = os.environ.get("ECS_CONTAINER_METADATA_URI_V4")
    if base:
        try:
            from urllib.request import urlopen

            with urlopen(base + "/task", timeout=2) as resp:
                return json.load(resp).get("TaskARN") or socket.gethostname()
        except Exception:
            pass
    return socket.gethostname()


def _error(user: str) -> bytes:
    fields = b"".join(
        k + v.encode() + b"\0"
        for k, v in (
            (b"S", "FATAL"),
            (b"V", "FATAL"),
            (b"C", "28P01"),
            (b"M", f'password authentication failed for user "{user}"'),
        )
    ) + b"\0"
    return b"E" + struct.pack("!I", len(fields) + 4) + fields


def parse_startup(body: bytes) -> Optional[Dict[str, str]]:
    """Key/value pairs of a StartupMessage body (after the protocol number), or None."""
    parts = body.split(b"\0")
    if len(parts) < 3 or parts[-1] != b"" or parts[-2] != b"" or len(parts) % 2 == 1:
        return None
    pairs = parts[:-2]
    return {pairs[i].decode("utf-8", "replace"): pairs[i + 1].decode("utf-8", "replace") for i in range(0, len(pairs), 2)}


class Listener:
    def __init__(self, sink: AlertSink, token_id: str, workspace_id: str, task: str = "",
                 startup_timeout: float = STARTUP_TIMEOUT, rate_window: float = RATE_WINDOW):
        self.sink, self.token_id, self.workspace_id = sink, token_id, workspace_id
        self.task = task or socket.gethostname()
        self.startup_timeout, self.rate_window = startup_timeout, rate_window
        self._last: Dict[str, float] = {}
        self.counts = {"trip": 0, "probe": 0, "suppressed": 0}

    async def _emit(self, kind: str, peer: tuple, info: Dict[str, str]) -> None:
        self.counts[kind] += 1
        ip, port = peer[0], peer[1]
        now = time.monotonic()
        if now - self._last.get(ip, -1e9) < self.rate_window:
            self.counts["suppressed"] += 1
            log.info("suppressed kind=%s ip=%s", kind, ip)
            return
        self._last[ip] = now
        event = {
            "token_id": self.token_id, "workspace_id": self.workspace_id, "kind": kind,
            "severity": "critical" if kind == "trip" else "low",
            "source_ip": ip, "source_port": port,
            "db_user": info.get("user", ""), "database": info.get("database", ""),
            "application_name": info.get("application_name", ""),
            "event_time": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "listener_task": self.task, "service": "db",
        }
        log.info("event kind=%s ip=%s user=%s database=%s", kind, ip, event["db_user"], event["database"])
        try:
            await asyncio.get_running_loop().run_in_executor(None, self.sink.send, event)
        except Exception:
            log.exception("alert send failed")

    async def _startup(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> Optional[Dict[str, str]]:
        for _ in range(3):  # SSL / GSS requests may precede the startup message
            length, code = struct.unpack("!II", await reader.readexactly(8))
            if code in (SSL_REQUEST, GSS_REQUEST) and length == 8:
                writer.write(b"N")
                await writer.drain()
                continue
            if code != PROTOCOL_V3 or not 8 < length <= MAX_STARTUP:
                return None
            return parse_startup(await reader.readexactly(length - 8))
        return None

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername") or ("unknown", 0)
        info: Optional[Dict[str, str]] = None
        try:
            info = await asyncio.wait_for(self._startup(reader, writer), self.startup_timeout)
            if info is None:
                await self._emit("probe", peer, {})
                return
            writer.write(b"R" + struct.pack("!II", 8, 3))
            await writer.drain()
            try:  # the password itself is read and discarded
                await asyncio.wait_for(reader.readexactly(1), self.startup_timeout)
                (length,) = struct.unpack("!I", await reader.readexactly(4))
                await reader.readexactly(min(max(length - 4, 0), MAX_STARTUP))
            except (asyncio.IncompleteReadError, asyncio.TimeoutError, ConnectionError):
                pass
            writer.write(_error(info.get("user", "")))
            await writer.drain()
            await self._emit("trip", peer, info)
        except (asyncio.IncompleteReadError, asyncio.TimeoutError, ConnectionError, struct.error):
            if info is None:
                await self._emit("probe", peer, {})
        except Exception:
            log.exception("connection failed")
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def serve(self, host: str, port: int, stop: Optional[asyncio.Event] = None) -> None:
        server = await asyncio.start_server(self.handle, host, port)
        log.info("listening on %s:%s", host, port)
        async with server:
            await (stop or asyncio.Event()).wait()
