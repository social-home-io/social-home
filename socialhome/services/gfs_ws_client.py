"""SH-side persistent WebSocket client for one paired GFS (spec §24.12).

The GFS is publicly reachable, so an SH instance opens a long-lived
``wss://`` connection to it for receiving relay events the GFS pushes.
SH→GFS calls (publish, subscribe, report, appeal) keep using the
existing REST endpoints in :class:`GfsConnectionService` — request /
response semantics fit those calls and a WS RPC envelope would only add
``request_id`` bookkeeping.

Lifecycle:

* :meth:`start` spawns the connect-and-listen loop in a background task.
* :meth:`stop` signals the loop to exit and awaits it (≤5 s).
* The loop reconnects with exponential backoff
  ``[1, 2, 4, 8, 30]`` seconds (clamped to the last value).
* On each connect: sends the signed hello frame
  ``{type:"hello", instance_id, ts, sig}``; thereafter dispatches every
  inbound ``{type:"relay", ...}`` frame to the injected ``on_relay``
  callable.

Heartbeat is WebSocket-protocol-level (aiohttp ``heartbeat=30``); the SH
never sends application frames after hello.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable

import aiohttp

from ..crypto import b64url_encode, sign_ed25519

log = logging.getLogger(__name__)


RECONNECT_DELAYS: tuple[float, ...] = (1.0, 2.0, 4.0, 8.0, 30.0)


def _to_ws_url(http_url: str) -> str:
    """Convert ``http(s)://host`` to the matching ``ws(s)://host/gfs/ws``."""
    base = http_url.rstrip("/")
    if base.startswith("https://"):
        return "wss://" + base[len("https://") :] + "/gfs/ws"
    if base.startswith("http://"):
        return "ws://" + base[len("http://") :] + "/gfs/ws"
    return base + "/gfs/ws"


class GfsWebSocketClient:
    """One persistent SH→GFS WebSocket connection.

    ``on_relay`` is invoked once per inbound ``relay`` frame; it must not
    raise — exceptions are caught and logged so a single bad frame cannot
    tear the loop down.
    """

    __slots__ = (
        "_gfs_url",
        "_instance_id",
        "_signing_key",
        "_session_factory",
        "_on_relay",
        "_reconnect_delays",
        "_stop",
        "_task",
        "_connected_event",
    )

    def __init__(
        self,
        *,
        gfs_url: str,
        instance_id: str,
        signing_key: bytes,
        session_factory: Callable[[], aiohttp.ClientSession],
        on_relay: Callable[[dict], Awaitable[None]],
        reconnect_delays: tuple[float, ...] = RECONNECT_DELAYS,
    ) -> None:
        self._gfs_url = gfs_url
        self._instance_id = instance_id
        self._signing_key = signing_key
        self._session_factory = session_factory
        self._on_relay = on_relay
        self._reconnect_delays = reconnect_delays
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._connected_event = asyncio.Event()

    # ─── Lifecycle ────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Start the background connect-and-listen loop. Idempotent."""
        if self._task is not None and not self._task.done():
            return
        self._stop.clear()
        self._connected_event.clear()
        self._task = asyncio.create_task(
            self._loop(),
            name=f"gfs-ws-client[{self._instance_id}->{self._gfs_url}]",
        )

    async def stop(self) -> None:
        """Stop the loop and wait for it to exit."""
        self._stop.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except asyncio.TimeoutError, asyncio.CancelledError:
                self._task.cancel()
            self._task = None

    @property
    def connected(self) -> bool:
        """``True`` while a WebSocket is currently open."""
        return self._connected_event.is_set()

    # ─── Internals ────────────────────────────────────────────────────────

    async def _loop(self) -> None:
        attempt = 0
        ws_url = _to_ws_url(self._gfs_url)
        while not self._stop.is_set():
            try:
                await self._run_once(ws_url)
                # Clean disconnect from the server side counts as a retry —
                # we want to come back up.
                attempt = 0
            except _GfsWsAuthFailure as exc:
                log.warning(
                    "gfs.ws.client: auth rejected by %s: %s — backing off",
                    self._gfs_url,
                    exc,
                )
            except Exception as exc:
                log.info(
                    "gfs.ws.client: connection to %s failed: %s",
                    self._gfs_url,
                    exc,
                )

            if self._stop.is_set():
                return
            delay = self._reconnect_delays[
                min(attempt, len(self._reconnect_delays) - 1)
            ]
            attempt += 1
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
                return  # _stop fired — exit loop
            except asyncio.TimeoutError:
                continue

    async def _run_once(self, ws_url: str) -> None:
        """One connect-attempt cycle. Returns on clean disconnect; raises on error."""
        session = self._session_factory()
        async with session.ws_connect(
            ws_url,
            heartbeat=30.0,
            max_msg_size=4 * 1024 * 1024,
        ) as ws:
            await ws.send_json(self._build_hello())
            self._connected_event.set()
            try:
                async for msg in ws:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        await self._on_text(msg.data)
                    elif msg.type == aiohttp.WSMsgType.ERROR:
                        log.warning(
                            "gfs.ws.client: socket error on %s: %s",
                            self._gfs_url,
                            ws.exception(),
                        )
                        break
                    elif msg.type == aiohttp.WSMsgType.CLOSE:
                        if ws.close_code in (4401, 4408, 4400):
                            raise _GfsWsAuthFailure(
                                f"server-closed code={ws.close_code}",
                            )
                        break
            finally:
                self._connected_event.clear()

    async def _on_text(self, raw: str) -> None:
        try:
            frame = json.loads(raw)
        except json.JSONDecodeError:
            log.warning(
                "gfs.ws.client: ignoring malformed JSON frame from %s",
                self._gfs_url,
            )
            return
        if not isinstance(frame, dict):
            return
        if frame.get("type") != "relay":
            log.debug(
                "gfs.ws.client: ignoring non-relay frame type=%r",
                frame.get("type"),
            )
            return
        try:
            await self._on_relay(frame)
        except Exception as exc:  # defensive
            log.warning(
                "gfs.ws.client: on_relay handler raised for %s: %s",
                self._gfs_url,
                exc,
            )

    def _build_hello(self) -> dict:
        ts = int(time.time())
        message = f"{self._instance_id}|{ts}".encode("utf-8")
        sig = sign_ed25519(self._signing_key, message)
        return {
            "type": "hello",
            "instance_id": self._instance_id,
            "ts": ts,
            "sig": b64url_encode(sig),
        }


class _GfsWsAuthFailure(Exception):
    """Raised when the GFS closes the connection with an auth-related code."""
