"""Tests for ``GfsWebSocketClient`` (spec §24.12, SH-side WS client)."""

from __future__ import annotations

import asyncio
import json
import time

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

from socialhome.crypto import b64url_decode, verify_ed25519
from socialhome.services.gfs_ws_client import GfsWebSocketClient, _to_ws_url


try:
    import pytest_socket  # noqa: F401

    @pytest.fixture(autouse=True)
    def _enable_sockets(socket_enabled):
        """Re-enable sockets if the HA pytest plugin disabled them.

        CI does not install ``pytest-socket``; on those runs this fixture
        is not registered and the test uses sockets normally.
        """

except ImportError:  # pragma: no cover - CI path
    pass


# ── Helpers ────────────────────────────────────────────────────────────────────


def _gen_keypair() -> tuple[bytes, bytes]:
    priv = ed25519.Ed25519PrivateKey.generate()
    seed = priv.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    pub = priv.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return seed, pub


# ── _to_ws_url ─────────────────────────────────────────────────────────────────


def test_to_ws_url_https():
    assert _to_ws_url("https://gfs.example.com") == "wss://gfs.example.com/gfs/ws"


def test_to_ws_url_http():
    assert _to_ws_url("http://localhost:8124") == "ws://localhost:8124/gfs/ws"


def test_to_ws_url_strips_trailing_slash():
    assert _to_ws_url("https://gfs.example.com/") == "wss://gfs.example.com/gfs/ws"


# ── In-process fake GFS WebSocket server ──────────────────────────────────────


class _FakeGfsServer:
    """Stub GFS that exposes ``/gfs/ws`` for the client to connect to.

    Records each hello frame and exposes a queue of relay frames the test
    can push to the client.
    """

    def __init__(self) -> None:
        self.hellos: list[dict] = []
        self.last_ws: web.WebSocketResponse | None = None
        self.connect_count: int = 0
        self.outbound: asyncio.Queue[dict] = asyncio.Queue()
        self.close_first_connect: bool = False
        self.close_code: int = 4401

    async def handler(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self.connect_count += 1
        msg = await ws.receive(timeout=5)
        self.hellos.append(json.loads(msg.data))
        if self.close_first_connect and self.connect_count == 1:
            await ws.close(code=self.close_code, message=b"reject")
            return ws
        self.last_ws = ws
        # Drain queued outbound frames for the duration of the connection.
        try:
            while not ws.closed:
                try:
                    frame = await asyncio.wait_for(
                        self.outbound.get(),
                        timeout=0.05,
                    )
                except asyncio.TimeoutError:
                    if ws.closed:
                        break
                    continue
                await ws.send_json(frame)
        except Exception:
            pass
        return ws


@pytest.fixture
async def fake_gfs():
    server_obj = _FakeGfsServer()
    app = web.Application()
    app.router.add_get("/gfs/ws", server_obj.handler)
    server = TestServer(app)
    await server.start_server()
    server_obj.url = str(server.make_url("/")).rstrip("/")
    yield server_obj
    await server.close()


@pytest.fixture
async def http_session():
    async with aiohttp.ClientSession() as session:
        yield session


# ── Tests ──────────────────────────────────────────────────────────────────────


async def test_client_sends_signed_hello(fake_gfs, http_session):
    seed, pub = _gen_keypair()
    relays: list[dict] = []

    async def on_relay(frame: dict) -> None:
        relays.append(frame)

    client = GfsWebSocketClient(
        gfs_url=fake_gfs.url,
        instance_id="sh-1",
        signing_key=seed,
        session_factory=lambda: http_session,
        on_relay=on_relay,
    )
    await client.start()
    try:
        for _ in range(100):
            if client.connected:
                break
            await asyncio.sleep(0.02)
        assert client.connected
        assert len(fake_gfs.hellos) == 1
        hello = fake_gfs.hellos[0]
        assert hello["type"] == "hello"
        assert hello["instance_id"] == "sh-1"
        assert isinstance(hello["ts"], int)
        assert abs(hello["ts"] - int(time.time())) < 5
        # Verify the signature against the test's pub key.
        sig = b64url_decode(hello["sig"])
        msg = f"sh-1|{hello['ts']}".encode("utf-8")
        assert verify_ed25519(pub, msg, sig)
    finally:
        await client.stop()


async def test_client_dispatches_inbound_relay(fake_gfs, http_session):
    seed, _pub = _gen_keypair()
    relays: list[dict] = []

    async def on_relay(frame: dict) -> None:
        relays.append(frame)

    client = GfsWebSocketClient(
        gfs_url=fake_gfs.url,
        instance_id="sh-2",
        signing_key=seed,
        session_factory=lambda: http_session,
        on_relay=on_relay,
    )
    await client.start()
    try:
        for _ in range(100):
            if client.connected:
                break
            await asyncio.sleep(0.02)
        await fake_gfs.outbound.put(
            {"type": "relay", "space_id": "s1", "payload": {"x": 1}},
        )
        for _ in range(100):
            if relays:
                break
            await asyncio.sleep(0.02)
        assert relays
        assert relays[0]["space_id"] == "s1"
        assert relays[0]["payload"] == {"x": 1}
    finally:
        await client.stop()


async def test_client_ignores_non_relay_frames(fake_gfs, http_session):
    seed, _pub = _gen_keypair()
    relays: list[dict] = []

    async def on_relay(frame: dict) -> None:
        relays.append(frame)

    client = GfsWebSocketClient(
        gfs_url=fake_gfs.url,
        instance_id="sh-3",
        signing_key=seed,
        session_factory=lambda: http_session,
        on_relay=on_relay,
    )
    await client.start()
    try:
        for _ in range(100):
            if client.connected:
                break
            await asyncio.sleep(0.02)
        await fake_gfs.outbound.put({"type": "noise", "msg": "ignored"})
        await fake_gfs.outbound.put(
            {"type": "relay", "space_id": "s2", "payload": {}},
        )
        for _ in range(100):
            if relays:
                break
            await asyncio.sleep(0.02)
        assert len(relays) == 1
        assert relays[0]["type"] == "relay"
        assert relays[0]["space_id"] == "s2"
    finally:
        await client.stop()


async def test_client_reconnects_with_backoff(fake_gfs, http_session):
    """When the GFS rejects the first connect, the client retries."""
    seed, _pub = _gen_keypair()
    fake_gfs.close_first_connect = True
    fake_gfs.close_code = 4401

    async def on_relay(frame: dict) -> None:
        pass

    client = GfsWebSocketClient(
        gfs_url=fake_gfs.url,
        instance_id="sh-4",
        signing_key=seed,
        session_factory=lambda: http_session,
        on_relay=on_relay,
        reconnect_delays=(0.05,),
    )
    await client.start()
    try:
        for _ in range(200):
            if fake_gfs.connect_count >= 2 and client.connected:
                break
            await asyncio.sleep(0.02)
        assert fake_gfs.connect_count >= 2
        assert client.connected
    finally:
        await client.stop()


async def test_client_stop_idempotent(fake_gfs, http_session):
    seed, _pub = _gen_keypair()

    async def on_relay(frame: dict) -> None:
        pass

    client = GfsWebSocketClient(
        gfs_url=fake_gfs.url,
        instance_id="sh-5",
        signing_key=seed,
        session_factory=lambda: http_session,
        on_relay=on_relay,
    )
    await client.start()
    await client.stop()
    await client.stop()  # second stop must not raise


async def test_client_handler_exception_does_not_kill_loop(fake_gfs, http_session):
    seed, _pub = _gen_keypair()
    seen: list[int] = []

    async def on_relay(frame: dict) -> None:
        seen.append(len(seen))
        if len(seen) == 1:
            raise RuntimeError("boom")

    client = GfsWebSocketClient(
        gfs_url=fake_gfs.url,
        instance_id="sh-6",
        signing_key=seed,
        session_factory=lambda: http_session,
        on_relay=on_relay,
    )
    await client.start()
    try:
        for _ in range(100):
            if client.connected:
                break
            await asyncio.sleep(0.02)
        await fake_gfs.outbound.put({"type": "relay", "n": 1})
        await fake_gfs.outbound.put({"type": "relay", "n": 2})
        for _ in range(100):
            if len(seen) == 2:
                break
            await asyncio.sleep(0.02)
        assert seen == [0, 1]  # second frame still dispatched after the boom
        assert client.connected
    finally:
        await client.stop()
