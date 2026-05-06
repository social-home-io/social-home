"""Recipient-side handler for incoming public moments (§Momentum-public).

Wired as a callback on the persistent SH↔GFS WebSocket. The GFS pushes
``incoming_public_moment`` and ``incoming_public_moment_delete`` frames
when an author the local user follows posts or deletes. This handler:

1. Verifies the envelope's Ed25519 signature against the cached
   ``followed_instance_pk`` for the (viewer, author, gfs) triple in
   :class:`MomentPublicFollow`. A bad signature is dropped silently
   (with a warning) — we never persist unauthenticated content.
2. Persists the moment with ``received_via='gfs'`` and
   ``received_via_gfs_id={gfs_id}`` so the inbox can render the chip
   AND so the federation outbound's relay path will skip it (no
   redistribute rule).
3. Re-publishes :class:`MomentCreated` on the bus so the existing
   realtime + notification surfaces light up exactly as for a
   household-relayed moment.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from ..crypto import b64url_decode, verify_ed25519
from ..domain.events import MomentCreated, MomentDeleted
from ..domain.moment import Moment
from ..infrastructure.event_bus import EventBus

if TYPE_CHECKING:
    from ..repositories.moment_public_repo import (
        AbstractMomentPublicFollowRepo,
    )
    from ..repositories.moment_repo import AbstractMomentRepo

log = logging.getLogger(__name__)


class MomentPublicInbound:
    __slots__ = ("_bus", "_moments", "_follows")

    def __init__(
        self,
        *,
        bus: EventBus,
        moment_repo: "AbstractMomentRepo",
        follow_repo: "AbstractMomentPublicFollowRepo",
    ) -> None:
        self._bus = bus
        self._moments = moment_repo
        self._follows = follow_repo

    async def handle(self, frame: dict[str, Any], *, gfs_id: str) -> None:
        """Dispatch a single WS frame from the GFS this handler is bound to."""
        ftype = frame.get("type")
        payload = frame.get("payload") or {}
        if ftype == "incoming_public_moment":
            await self._on_create(payload, gfs_id=gfs_id)
        elif ftype == "incoming_public_moment_delete":
            await self._on_delete(payload, gfs_id=gfs_id)
        else:
            log.debug("moment_public.inbound: ignoring frame type=%r", ftype)

    async def _on_create(self, env: dict, *, gfs_id: str) -> None:
        if not _verify(env, await self._lookup_pk(env, gfs_id=gfs_id)):
            log.warning(
                "moment_public.inbound: signature failed moment=%s author=%s",
                env.get("moment_id"),
                env.get("author_user_id"),
            )
            return
        moment = Moment(
            id=str(env["moment_id"]),
            author_user_id=str(env["author_user_id"]),
            content=str(env.get("content") or ""),
            media_url=env.get("media_url"),
            media_type=env.get("media_type"),
            duration_ms=env.get("duration_ms"),
            parent_moment_id=env.get("parent_moment_id"),
            origin_instance_id=str(env.get("origin_instance_id") or ""),
            created_at=str(env["created_at"]),
            expires_at=str(env["expires_at"]),
            is_public=True,
            received_via="gfs",
            received_via_gfs_id=gfs_id,
        )
        await self._moments.save(moment)
        await self._bus.publish(
            MomentCreated(
                moment_id=moment.id,
                author_user_id=moment.author_user_id,
                content=moment.content,
                media_url=moment.media_url,
                media_type=moment.media_type,
                duration_ms=moment.duration_ms,
                parent_moment_id=moment.parent_moment_id,
                # Reply parent's author isn't carried on the wire — the
                # parent moment's row (if local) holds it. NULL is fine
                # for notification routing.
                parent_author_user_id=None,
                origin_instance_id=moment.origin_instance_id,
                expires_at=moment.expires_at,
            )
        )

    async def _on_delete(self, env: dict, *, gfs_id: str) -> None:
        # Deletes still need a signature check so a malicious peer
        # can't take down a moment they don't own.
        author_pk = await self._lookup_pk(env, gfs_id=gfs_id)
        if not _verify(env, author_pk):
            log.warning(
                "moment_public.inbound: delete signature failed for %s",
                env.get("moment_id"),
            )
            return
        moment_id = str(env.get("moment_id") or "")
        if not moment_id:
            return
        existing = await self._moments.get(moment_id)
        if existing is None:
            return
        await self._moments.delete(moment_id)
        await self._bus.publish(
            MomentDeleted(
                moment_id=moment_id,
                author_user_id=str(env.get("author_user_id") or ""),
                origin_instance_id=existing.origin_instance_id,
            )
        )

    async def _lookup_pk(self, env: dict, *, gfs_id: str) -> str | None:
        author = str(env.get("author_user_id") or "")
        if not author:
            return None
        # Any local follower of this author records the author's pk;
        # find the first row that matches on (followed=author, gfs=gfs_id).
        for row in await self._follows.followers_of(author):
            if row.gfs_id == gfs_id:
                return row.followed_instance_pk
        return None


def _verify(envelope: dict, pubkey_hex: str | None) -> bool:
    sig = envelope.get("signature")
    if not sig or not pubkey_hex:
        return False
    body = {k: v for k, v in envelope.items() if k != "signature"}
    canonical = json.dumps(body, separators=(",", ":"), sort_keys=True).encode("utf-8")
    try:
        return verify_ed25519(
            bytes.fromhex(pubkey_hex), canonical, b64url_decode(str(sig))
        )
    except ValueError, TypeError:
        return False
