"""GFS WebSocket registry — one open ``wss://`` per paired SH instance.

Spec §24.12. Each paired Social Home household keeps a persistent
WebSocket against ``GET /gfs/ws``; the registry maps ``instance_id`` to
the live :class:`aiohttp.web.WebSocketResponse` so :class:`GfsFederationService`
can push relay events directly to subscribers without an HTTP callback.

Single-connection-per-instance: when a new connection arrives for an
``instance_id`` that already has one open, the old socket is evicted with
WebSocket close code ``4409`` so the new connection takes over cleanly
(typical case: the household process restarted faster than the existing
socket's TCP keepalive could detect).

Send semantics: :meth:`send` returns ``True`` when delivery to the live
socket succeeded and ``False`` if no socket is registered or the send
failed; the caller (fan-out) then falls back to the HTTPS inbox path.
Dead sockets are evicted on send failure so a single broken peer cannot
block fan-out.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from aiohttp import web

log = logging.getLogger(__name__)


_EVICT_REPLACED_CODE = 4409  # private-use range; "Replaced by newer connection"


class GfsWebSocketRegistry:
    """Per-instance registry of live GFS-side WebSocket sessions."""

    __slots__ = ("_by_instance", "_lock")

    def __init__(self) -> None:
        self._by_instance: dict[str, web.WebSocketResponse] = {}
        self._lock = asyncio.Lock()

    # ─── Lifecycle ────────────────────────────────────────────────────────

    async def register(
        self,
        instance_id: str,
        ws: web.WebSocketResponse,
    ) -> None:
        """Register *ws* for *instance_id*, evicting any prior socket."""
        async with self._lock:
            previous = self._by_instance.get(instance_id)
            self._by_instance[instance_id] = ws
        if previous is not None and previous is not ws and not previous.closed:
            try:
                await previous.close(
                    code=_EVICT_REPLACED_CODE,
                    message=b"replaced",
                )
            except Exception as exc:  # defensive — never block new registration
                log.debug("ws.evict failed instance=%s: %s", instance_id, exc)
        log.info(
            "gfs.ws.register: instance=%s total=%d",
            instance_id,
            self.connection_count(),
        )

    async def unregister(
        self,
        instance_id: str,
        ws: web.WebSocketResponse,
    ) -> None:
        """Drop *ws* if it is the currently-tracked socket for *instance_id*."""
        async with self._lock:
            current = self._by_instance.get(instance_id)
            if current is ws:
                self._by_instance.pop(instance_id, None)
        log.info(
            "gfs.ws.unregister: instance=%s total=%d",
            instance_id,
            self.connection_count(),
        )

    async def close_all(self) -> None:
        """Close every tracked socket — called from the GFS cleanup hook."""
        async with self._lock:
            sockets = list(self._by_instance.values())
            self._by_instance.clear()
        for ws in sockets:
            if ws.closed:
                continue
            try:
                await ws.close(code=1001, message=b"server-shutdown")
            except Exception as exc:
                log.debug("gfs.ws.close_all: socket close failed: %s", exc)

    # ─── Inspection ───────────────────────────────────────────────────────

    def connection_count(self) -> int:
        return len(self._by_instance)

    def is_connected(self, instance_id: str) -> bool:
        ws = self._by_instance.get(instance_id)
        return ws is not None and not ws.closed

    def connected_instances(self) -> set[str]:
        return set(self._by_instance.keys())

    # ─── Push ─────────────────────────────────────────────────────────────

    async def send(self, instance_id: str, payload: dict[str, Any]) -> bool:
        """Push a JSON frame to *instance_id*.

        Returns ``True`` on successful send, ``False`` when no socket is
        registered or the send failed (in which case the dead socket is
        evicted and the caller can fall back to the HTTPS inbox path).
        """
        ws = self._by_instance.get(instance_id)
        if ws is None or ws.closed:
            return False
        msg = json.dumps(payload, default=str)
        try:
            await ws.send_str(msg)
            return True
        except ConnectionResetError, RuntimeError, asyncio.CancelledError:
            await self._drop_dead(instance_id, ws)
            return False
        except Exception as exc:  # defensive
            log.debug("gfs.ws.send failed instance=%s: %s", instance_id, exc)
            await self._drop_dead(instance_id, ws)
            return False

    async def _drop_dead(
        self,
        instance_id: str,
        ws: web.WebSocketResponse,
    ) -> None:
        async with self._lock:
            if self._by_instance.get(instance_id) is ws:
                self._by_instance.pop(instance_id, None)
