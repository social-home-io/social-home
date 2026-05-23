"""Provider-side space sync (§25.6).

:class:`SpaceSyncService.stream_initial` walks :data:`RESOURCE_ORDER`,
paginates each resource via its exporter, encrypts + signs chunks,
and writes them to the DataChannel. Emits a final
``__complete__`` sentinel when done.

:class:`SpaceSyncService.stream_request_more` streams the slice asked
for by a peer's ``SPACE_SYNC_REQUEST_MORE`` event (after S-12 clamping).

Callers fire-and-forget via ``asyncio.create_task``; the session
record tracks the task so :class:`SyncSessionManager.close_session`
can cancel mid-stream if the peer gives up.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Any, TYPE_CHECKING

from .exporter import ChunkBuilder, RESOURCE_ORDER, serialise_chunk

if TYPE_CHECKING:
    from ...sync_manager import SyncSessionRecord
    from .exporter import ResourceExporter

log = logging.getLogger(__name__)


class SpaceSyncService:
    """Streams encrypted space content over a negotiated DataChannel."""

    __slots__ = (
        "_builder",
        "_exporters",
        "_sig_suite",
        "_media_sync",
        "_space_post_repo",
        "_gallery_repo",
        "_bazaar_repo",
    )

    def __init__(
        self,
        *,
        builder: ChunkBuilder,
        exporters: dict[str, "ResourceExporter"],
        sig_suite: str = "ed25519",
        media_sync=None,
        space_post_repo=None,
        gallery_repo=None,
        bazaar_repo=None,
    ) -> None:
        self._builder = builder
        self._exporters = exporters
        self._sig_suite = sig_suite
        #: Optional — when wired, the provider enqueues bytes for
        #: every post / gallery / bazaar media URL after the metadata
        #: chunks stream, so a catch-up sync ALSO ships the historical
        #: images (not just the rows). Without it the receiver gets
        #: post + gallery metadata but renders broken thumbnails.
        self._media_sync = media_sync
        self._space_post_repo = space_post_repo
        self._gallery_repo = gallery_repo
        self._bazaar_repo = bazaar_repo

    async def stream_initial(self, session: "SyncSessionRecord") -> None:
        """Send every resource for ``session.space_id`` over the channel
        in :data:`RESOURCE_ORDER`, then a ``__complete__`` sentinel.

        Safe to call from ``asyncio.create_task`` — exceptions are
        logged, not re-raised. Callers rely on the session record's
        task slot to cancel this if the channel dies.
        """
        sync_id = session.sync_id
        space_id = session.space_id
        try:
            for resource in RESOURCE_ORDER:
                exporter = self._exporters.get(resource)
                if exporter is None:
                    log.debug("no exporter for resource %s — skipping", resource)
                    continue
                async for envelope in self._builder.build_chunks(
                    exporter=exporter,
                    space_id=space_id,
                    sync_id=sync_id,
                    sig_suite=self._sig_suite,
                ):
                    await _send(session, envelope)
            sentinel = await self._builder.build_sentinel(
                space_id=space_id,
                sync_id=sync_id,
                sig_suite=self._sig_suite,
            )
            await _send(session, sentinel)
            # Catch-up media: enumerate every post + gallery item in
            # the space, collect their media URLs, and enqueue
            # ``space_media_outbox`` rows so the requesting peer
            # receives the bytes via the same scheduler that real-time
            # uploads use. The receiver lands them on its media path
            # under the SAME filename the metadata referenced, so the
            # rendered ``<img src>`` resolves the moment the chunks
            # finish landing. The rows are bounded — same blob to
            # same peer dedups at the ON CONFLICT primary key.
            await self._enqueue_catchup_media(
                space_id,
                session.requester_instance_id,
            )
        except Exception:  # pragma: no cover
            log.exception(
                "stream_initial failed for sync_id=%s space=%s",
                sync_id,
                space_id,
            )

    async def _enqueue_catchup_media(
        self,
        space_id: str,
        target_instance_id: str,
    ) -> None:
        """Enqueue media bytes for every post + gallery item in ``space_id``.

        Repo reads catch :class:`sqlite3.Error` only — a renamed /
        missing repo method raises ``AttributeError`` (a logic bug),
        which propagates up to ``stream_initial``'s outer handler so
        it surfaces in logs as a single visible failure rather than
        being silently swallowed per-call. The original wide
        ``except Exception`` here masked exactly this kind of bug
        (see #443 — ``list_items_for_space`` never existed; tests
        mocked whatever was called so the gap never showed up). Same
        dedup semantics as the realtime enqueue: the ``(blob_id,
        target_instance_id)`` primary key drops duplicates.
        """
        if self._media_sync is None or target_instance_id == "":
            return
        # Posts
        if self._space_post_repo is not None:
            try:
                posts = await self._space_post_repo.list_feed(
                    space_id,
                    limit=1000,
                )
            except sqlite3.Error:
                log.exception(
                    "sync-catchup-media: list posts failed for space=%s",
                    space_id,
                )
                posts = []
            for post in posts:
                urls = self._post_media_urls(post)
                if not urls:
                    continue
                try:
                    await self._media_sync.enqueue_for_blob(
                        space_id=space_id,
                        correlation_id=post.id,
                        target_instance_ids=[target_instance_id],
                        media_urls=urls,
                    )
                except sqlite3.Error:
                    # One outbox-insert hitting a transient SQLite
                    # error (lock, disk full) shouldn't kill the whole
                    # loop — the next sync will re-enqueue.
                    log.exception(
                        "sync-catchup-media: enqueue failed for post=%s",
                        post.id,
                    )
        # Gallery items — enumerate every album in the space, then every
        # item per album. The repo API is per-album (no list_items_for_space
        # shortcut); we walk both levels so a space with multiple albums
        # catches up cleanly.
        if self._gallery_repo is not None:
            items: list = []
            try:
                albums = await self._gallery_repo.list_albums(
                    space_id,
                    limit=200,
                )
                for album in albums:
                    page = await self._gallery_repo.list_items(
                        album.id,
                        limit=500,
                    )
                    items.extend(page)
            except sqlite3.Error:
                log.exception(
                    "sync-catchup-media: list gallery items failed for space=%s",
                    space_id,
                )
                items = []
            for item in items:
                gallery_urls: list[str] = []
                if getattr(item, "thumbnail_url", None):
                    gallery_urls.append(item.thumbnail_url)
                if getattr(item, "url", None) and item.url != item.thumbnail_url:
                    gallery_urls.append(item.url)
                if not gallery_urls:
                    continue
                try:
                    await self._media_sync.enqueue_for_blob(
                        space_id=space_id,
                        correlation_id=item.id,
                        target_instance_ids=[target_instance_id],
                        media_urls=gallery_urls,
                    )
                except sqlite3.Error:
                    log.exception(
                        "sync-catchup-media: enqueue failed for gallery item=%s",
                        item.id,
                    )
        # Bazaar listings — each listing's photos live on
        # ``BazaarListing.image_urls`` (NOT on the wrapper Post). Without
        # this walk a remote member sees the wrapper ``PostType.BAZAAR``
        # post via ``SPACE_POST_CREATED`` catch-up but the listing
        # row stays empty and the photos render broken. Same dedup +
        # correlation_id semantics as posts; ``listing.post_id`` is
        # used as the correlation so the realtime + catch-up enqueues
        # collide cleanly at the outbox PK.
        if self._bazaar_repo is not None:
            try:
                listings = await self._bazaar_repo.list_in_space(
                    space_id,
                    limit=500,
                )
            except sqlite3.Error:
                log.exception(
                    "sync-catchup-media: list bazaar listings failed for space=%s",
                    space_id,
                )
                listings = []
            for listing in listings:
                if not listing.image_urls:
                    continue
                try:
                    await self._media_sync.enqueue_for_blob(
                        space_id=space_id,
                        correlation_id=listing.post_id,
                        target_instance_ids=[target_instance_id],
                        media_urls=list(listing.image_urls),
                    )
                except sqlite3.Error:
                    log.exception(
                        "sync-catchup-media: enqueue failed for bazaar listing=%s",
                        listing.post_id,
                    )

    @staticmethod
    def _post_media_urls(post) -> list[str]:
        urls: list[str] = []
        if getattr(post, "media_url", None):
            urls.append(post.media_url)
        urls.extend(getattr(post, "image_urls", None) or ())
        fm = getattr(post, "file_meta", None)
        if fm is not None and getattr(fm, "url", None):
            urls.append(fm.url)
        return urls

    async def stream_request_more(
        self,
        session: "SyncSessionRecord",
        cleaned: dict[str, Any],
    ) -> None:
        """Stream the specific resource slice the peer asked for.

        ``cleaned`` is the output of ``sync_manager.clamp_request_more``:
        already validated to be one of :data:`ALLOWED_RESOURCES` within
        sane bounds.
        """
        resource = str(cleaned.get("resource") or "")
        exporter = self._exporters.get(resource)
        if exporter is None:
            log.debug(
                "REQUEST_MORE for %s has no exporter — skipping",
                resource,
            )
            return
        try:
            async for envelope in self._builder.build_chunks(
                exporter=exporter,
                space_id=session.space_id,
                sync_id=session.sync_id,
                sig_suite=self._sig_suite,
            ):
                await _send(session, envelope)
        except Exception:  # pragma: no cover
            log.exception(
                "stream_request_more failed for sync_id=%s resource=%s",
                session.sync_id,
                resource,
            )


async def _send(session, envelope: dict[str, Any]) -> None:
    """Serialise and dispatch one envelope over the DataChannel."""
    rtc = getattr(session, "rtc", None)
    if rtc is None:
        raise RuntimeError(
            f"SyncSessionRecord {session.sync_id} has no rtc handle",
        )
    await rtc.send_chunk(serialise_chunk(envelope))
