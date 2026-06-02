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


async def test_client_dispatches_moment_public_frames(fake_gfs, http_session):
    """``incoming_public_moment`` + ``incoming_public_moment_delete``
    frames go to the dedicated handler."""
    seed, _pub = _gen_keypair()
    moments: list[dict] = []
    follows: list[dict] = []

    async def on_relay(frame: dict) -> None:
        pass

    async def on_moment(frame: dict) -> None:
        moments.append(frame)

    async def on_follow(frame: dict) -> None:
        follows.append(frame)

    client = GfsWebSocketClient(
        gfs_url=fake_gfs.url,
        instance_id="sh-mp",
        signing_key=seed,
        session_factory=lambda: http_session,
        on_relay=on_relay,
        on_moment_public=on_moment,
        on_follow_changed=on_follow,
    )
    await client.start()
    try:
        for _ in range(100):
            if client.connected:
                break
            await asyncio.sleep(0.02)
        await fake_gfs.outbound.put(
            {"type": "incoming_public_moment", "payload": {"moment_id": "m-1"}},
        )
        await fake_gfs.outbound.put(
            {
                "type": "incoming_public_moment_delete",
                "payload": {"moment_id": "m-1"},
            },
        )
        await fake_gfs.outbound.put(
            {
                "type": "follow_changed",
                "action": "add",
                "follower_user_id": "u-2",
                "follower_instance_id": "inst-2",
                "followed_user_id": "u-1",
            },
        )
        for _ in range(100):
            if len(moments) >= 2 and follows:
                break
            await asyncio.sleep(0.02)
        assert len(moments) == 2
        assert moments[0]["type"] == "incoming_public_moment"
        assert moments[1]["type"] == "incoming_public_moment_delete"
        assert len(follows) == 1
        assert follows[0]["action"] == "add"
    finally:
        await client.stop()


async def test_client_dispatches_moment_signal_frames(fake_gfs, http_session):
    """``moment_signal`` frames (the §Momentum-public answerer) go to the
    dedicated handler, mirroring the highlight_signal branch."""
    seed, _pub = _gen_keypair()
    signals: list[dict] = []

    async def on_relay(frame: dict) -> None:
        pass

    async def on_moment_signal(frame: dict) -> None:
        signals.append(frame)

    client = GfsWebSocketClient(
        gfs_url=fake_gfs.url,
        instance_id="sh-ms",
        signing_key=seed,
        session_factory=lambda: http_session,
        on_relay=on_relay,
        on_moment_signal=on_moment_signal,
    )
    await client.start()
    try:
        for _ in range(100):
            if client.connected:
                break
            await asyncio.sleep(0.02)
        await fake_gfs.outbound.put(
            {
                "type": "moment_signal",
                "kind": "offer",
                "session_id": "s-1",
                "user_id": "u-1",
                "gfs_id": "gfs-1",
                "sdp": "v=0",
            },
        )
        for _ in range(100):
            if signals:
                break
            await asyncio.sleep(0.02)
        assert len(signals) == 1
        assert signals[0]["type"] == "moment_signal"
        assert signals[0]["kind"] == "offer"
        assert signals[0]["user_id"] == "u-1"
    finally:
        await client.stop()


async def test_client_attach_moment_signal_handler_late_binds(fake_gfs, http_session):
    """``attach_moment_signal_handler`` wires the answerer after construction."""
    seed, _pub = _gen_keypair()
    signals: list[dict] = []

    async def on_relay(frame: dict) -> None:
        pass

    async def handler(frame: dict) -> None:
        signals.append(frame)

    client = GfsWebSocketClient(
        gfs_url=fake_gfs.url,
        instance_id="sh-ms2",
        signing_key=seed,
        session_factory=lambda: http_session,
        on_relay=on_relay,
    )
    client.attach_moment_signal_handler(handler)
    await client.start()
    try:
        for _ in range(100):
            if client.connected:
                break
            await asyncio.sleep(0.02)
        await fake_gfs.outbound.put(
            {"type": "moment_signal", "kind": "ice", "session_id": "s"},
        )
        for _ in range(100):
            if signals:
                break
            await asyncio.sleep(0.02)
        assert len(signals) == 1
    finally:
        await client.stop()


async def test_client_drops_moment_signal_when_no_handler(fake_gfs, http_session):
    """No-handler path is a debug log, not a crash."""
    seed, _pub = _gen_keypair()
    relays: list[dict] = []

    async def on_relay(frame: dict) -> None:
        relays.append(frame)

    client = GfsWebSocketClient(
        gfs_url=fake_gfs.url,
        instance_id="sh-ms-noop",
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
            {"type": "moment_signal", "kind": "offer", "session_id": "s"},
        )
        await fake_gfs.outbound.put({"type": "relay", "space_id": "s", "payload": {}})
        for _ in range(100):
            if relays:
                break
            await asyncio.sleep(0.02)
        # Relay still dispatched, moment_signal dropped silently.
        assert len(relays) == 1
    finally:
        await client.stop()


async def test_client_drops_moment_public_when_no_handler(fake_gfs, http_session):
    """No-handler path is a debug log, not a crash."""
    seed, _pub = _gen_keypair()
    relays: list[dict] = []

    async def on_relay(frame: dict) -> None:
        relays.append(frame)

    client = GfsWebSocketClient(
        gfs_url=fake_gfs.url,
        instance_id="sh-noop",
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
            {"type": "incoming_public_moment", "payload": {"moment_id": "m-1"}},
        )
        await fake_gfs.outbound.put(
            {"type": "relay", "space_id": "s", "payload": {}},
        )
        for _ in range(100):
            if relays:
                break
            await asyncio.sleep(0.02)
        # Relay still got dispatched, moment_public dropped silently.
        assert len(relays) == 1
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
