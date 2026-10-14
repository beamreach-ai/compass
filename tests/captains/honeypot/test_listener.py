"""The decoy database listener, driven with raw protocol bytes over real sockets."""
from __future__ import annotations

import asyncio
import struct

import pytest

from captains.honeypot.listener.server import AlertSink, Listener


class FakeSink(AlertSink):
    def __init__(self):
        super().__init__("http://queue")
        self.events = []

    def send(self, event):
        self.events.append(event)


def startup(user="svc_backup", database="payments", app="psql") -> bytes:
    body = struct.pack("!I", 196608)
    for k, v in (("user", user), ("database", database), ("application_name", app)):
        body += k.encode() + b"\0" + v.encode() + b"\0"
    body += b"\0"
    return struct.pack("!I", len(body) + 4) + body


PASSWORD = b"p" + struct.pack("!I", 4 + 7) + b"secret\0"
SSL = struct.pack("!II", 8, 80877103)
GSS = struct.pack("!II", 8, 80877104)


async def _run(scenario, **kw):
    sink = FakeSink()
    listener = Listener(sink, "tok-1", "ws-1", task="task-x", **kw)
    server = await asyncio.start_server(listener.handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        await scenario(port, sink, listener)
    finally:
        server.close()
        await server.wait_closed()
    return sink, listener


async def _read_all(reader) -> bytes:
    return await asyncio.wait_for(reader.read(), 5)


def _parse_error(data: bytes) -> dict:
    assert data[:1] == b"E"
    (length,) = struct.unpack("!I", data[1:5])
    assert length == len(data) - 1
    fields = {}
    for part in data[5:-1].split(b"\0"):
        if part:
            fields[chr(part[0])] = part[1:].decode()
    return fields


async def _settle(sink, n=1):
    for _ in range(100):
        if len(sink.events) >= n:
            return
        await asyncio.sleep(0.02)


def test_full_login_attempt_is_one_critical_trip():
    async def scenario(port, sink, _l):
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(startup())
        await writer.drain()
        assert await reader.readexactly(9) == b"R" + struct.pack("!II", 8, 3)
        writer.write(PASSWORD)
        await writer.drain()
        err = _parse_error(await _read_all(reader))
        assert err["C"] == "28P01" and err["S"] == "FATAL" and err["V"] == "FATAL"
        assert err["M"] == 'password authentication failed for user "svc_backup"'
        writer.close()
        await _settle(sink)

    sink, _ = asyncio.run(_run(scenario))
    assert len(sink.events) == 1
    event = sink.events[0]
    assert event["kind"] == "trip" and event["severity"] == "critical" and event["service"] == "db"
    assert event["token_id"] == "tok-1" and event["workspace_id"] == "ws-1"
    assert event["source_ip"] == "127.0.0.1" and isinstance(event["source_port"], int)
    assert (event["db_user"], event["database"], event["application_name"]) == ("svc_backup", "payments", "psql")
    assert event["listener_task"] == "task-x"
    assert event["event_time"].endswith("Z") and len(event["event_time"]) == 24


@pytest.mark.parametrize("prelude", [SSL, GSS, GSS + SSL])
def test_ssl_and_gss_requests_get_n_then_startup_continues(prelude):
    async def scenario(port, sink, _l):
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(prelude)
        await writer.drain()
        assert await reader.readexactly(len(prelude) // 8) == b"N" * (len(prelude) // 8)
        writer.write(startup())
        assert (await reader.readexactly(1)) == b"R"
        await reader.readexactly(8)
        writer.write(PASSWORD)
        _parse_error(await _read_all(reader))
        writer.close()
        await _settle(sink)

    sink, _ = asyncio.run(_run(scenario))
    assert [e["kind"] for e in sink.events] == ["trip"]


def test_bare_connect_that_closes_is_a_low_probe():
    async def scenario(port, sink, _l):
        _, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.close()
        await _settle(sink)

    sink, _ = asyncio.run(_run(scenario))
    assert [(e["kind"], e["severity"]) for e in sink.events] == [("probe", "low")]
    assert sink.events[0]["db_user"] == ""


def test_silent_connection_times_out_as_probe():
    async def scenario(port, sink, _l):
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        assert await asyncio.wait_for(reader.read(), 5) == b""
        await _settle(sink)
        writer.close()

    sink, _ = asyncio.run(_run(scenario, startup_timeout=0.2))
    assert [e["kind"] for e in sink.events] == ["probe"]


@pytest.mark.parametrize("garbage", [b"GET / HTTP/1.1\r\n\r\n", b"\x00" * 64, struct.pack("!II", 12, 12345) + b"abcd"])
def test_garbage_is_a_probe_and_gets_no_reply(garbage):
    async def scenario(port, sink, _l):
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(garbage)
        await writer.drain()
        assert await _read_all(reader) == b""
        await _settle(sink)
        writer.close()

    sink, _ = asyncio.run(_run(scenario))
    assert [e["kind"] for e in sink.events] == ["probe"]


def test_rate_limit_one_event_per_ip_but_everything_is_counted():
    async def scenario(port, sink, listener):
        for _ in range(5):
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(startup())
            await reader.readexactly(9)
            writer.write(PASSWORD)
            _parse_error(await _read_all(reader))
            writer.close()
        for _ in range(100):
            if listener.counts["trip"] == 5:
                break
            await asyncio.sleep(0.02)

    sink, listener = asyncio.run(_run(scenario))
    assert len(sink.events) == 1
    assert listener.counts["trip"] == 5 and listener.counts["suppressed"] == 4


def test_rate_limit_window_expires_and_other_sources_are_independent():
    async def scenario(port, sink, listener):
        listener._last["10.0.0.9"] = 1e18  # another source already inside its window
        for _ in range(2):
            _, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.close()
            await asyncio.sleep(0.15)
        await _settle(sink, 2)

    sink, _ = asyncio.run(_run(scenario, rate_window=0.1))
    assert len(sink.events) == 2


def test_sink_failure_does_not_break_the_connection():
    class Broken(FakeSink):
        def send(self, event):
            raise RuntimeError("queue down")

    async def go():
        listener = Listener(Broken(), "t", "w")
        server = await asyncio.start_server(listener.handle, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(startup())
        await reader.readexactly(9)
        writer.write(PASSWORD)
        assert _parse_error(await _read_all(reader))["C"] == "28P01"
        writer.close()
        server.close()

    asyncio.run(go())
