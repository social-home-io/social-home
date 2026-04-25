"""Background supervisor that keeps one ``GfsWebSocketClient`` per pairing.

Spec §24.12. The set of paired GFSes can change at any time (admin
pairs a new one via the UI, or disconnects an existing one). The
supervisor periodically reconciles the live client set against
``gfs_connection_repo.list_active()`` and starts / stops clients to
match.

Lifecycle follows the project-standard ``_stop: asyncio.Event`` pattern
(reference: :class:`socialhome.infrastructure.replay_cache_scheduler.ReplayCachePruneScheduler`).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

import aiohttp

from ..domain.federation import GfsConnection
from ..repositories.gfs_connection_repo import AbstractGfsConnectionRepo
from ..services.gfs_ws_client import GfsWebSocketClient

log = logging.getLogger(__name__)


DEFAULT_RECONCILE_INTERVAL_SECONDS = 30.0


class GfsWebSocketSupervisor:
    """Owns the per-pairing :class:`GfsWebSocketClient` set.

    The supervisor never blocks a public method on a network call —
    starts and stops all run on the background loop.
    """

    __slots__ = (
        "_repo",
        "_instance_id",
        "_signing_key",
        "_session_factory",
        "_on_relay",
        "_interval",
        "_clients",
        "_lock",
        "_stop",
        "_task",
    )

    def __init__(
        self,
        *,
        repo: AbstractGfsConnectionRepo,
        instance_id: str,
        signing_key: bytes,
        session_factory: Callable[[], aiohttp.ClientSession],
        on_relay: Callable[[dict], Awaitable[None]],
        reconcile_interval_seconds: float = DEFAULT_RECONCILE_INTERVAL_SECONDS,
    ) -> None:
        self._repo = repo
        self._instance_id = instance_id
        self._signing_key = signing_key
        self._session_factory = session_factory
        self._on_relay = on_relay
        self._interval = reconcile_interval_seconds
        self._clients: dict[str, GfsWebSocketClient] = {}
        self._lock = asyncio.Lock()
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None

    # ─── Lifecycle ────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Run an immediate reconcile and spawn the background loop."""
        if self._task is not None and not self._task.done():
            return
        self._stop.clear()
        await self._reconcile_once()
        self._task = asyncio.create_task(self._loop(), name="gfs-ws-supervisor")

    async def stop(self) -> None:
        """Stop every client and the reconcile loop."""
        self._stop.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except asyncio.TimeoutError, asyncio.CancelledError:
                self._task.cancel()
            self._task = None
        async with self._lock:
            clients = list(self._clients.values())
            self._clients.clear()
        for client in clients:
            await client.stop()

    # ─── Inspection ───────────────────────────────────────────────────────

    def client_count(self) -> int:
        return len(self._clients)

    def is_running(self, gfs_id: str) -> bool:
        return gfs_id in self._clients

    # ─── Reconciliation ───────────────────────────────────────────────────

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(
                    self._stop.wait(),
                    timeout=self._interval,
                )
                return  # _stop fired
            except asyncio.TimeoutError:
                pass
            try:
                await self._reconcile_once()
            except Exception as exc:  # pragma: no cover - defensive
                log.warning("gfs.ws.supervisor: reconcile failed: %s", exc)

    async def _reconcile_once(self) -> None:
        """Sync the running-client set with the repo's active pairings."""
        active = await self._repo.list_active()
        active_ids = {c.id for c in active}
        async with self._lock:
            current_ids = set(self._clients.keys())
        to_start = [c for c in active if c.id not in current_ids]
        to_stop_ids = current_ids - active_ids

        for conn in to_start:
            await self._start_client(conn)
        for gfs_id in to_stop_ids:
            await self._stop_client(gfs_id)

    async def _start_client(self, conn: GfsConnection) -> None:
        client = GfsWebSocketClient(
            gfs_url=conn.inbox_url,
            instance_id=self._instance_id,
            signing_key=self._signing_key,
            session_factory=self._session_factory,
            on_relay=self._on_relay,
        )
        async with self._lock:
            existing = self._clients.get(conn.id)
            self._clients[conn.id] = client
        if existing is not None:
            await existing.stop()
        await client.start()
        log.info(
            "gfs.ws.supervisor: started client gfs_id=%s url=%s",
            conn.id,
            conn.inbox_url,
        )

    async def _stop_client(self, gfs_id: str) -> None:
        async with self._lock:
            client = self._clients.pop(gfs_id, None)
        if client is not None:
            await client.stop()
            log.info("gfs.ws.supervisor: stopped client gfs_id=%s", gfs_id)
