"""Outbound federation for personal Highlights (§Highlights).

Subscribes to :class:`HighlightFrameAdded`, :class:`HighlightFrameRemoved` and
:class:`HighlightRemoved` and translates the local-write events into the
matching ``HIGHLIGHT_*`` federation envelopes targeted at the audience.

Audience routing — Highlights are *not* space-scoped:

* ``audience_kind = 'all_paired'`` → fan to every confirmed peer.
* ``audience_kind = 'households'`` → fan to the listed peer instance ids
  only (already in instance-id form on the event payload).
* ``audience_kind = 'users'``      → resolve each ``audience`` user id
  to its home instance via :class:`AbstractUserRepo` and dedup.

**Echo-loop guard.** :class:`FederationInboundService` republishes
``HighlightFrameAdded`` / ``HighlightRemoved`` / ``HighlightFrameRemoved`` on the
local bus so :class:`RealtimeService` can fan to local viewers. To
avoid re-fanning that event back to every peer, this subscriber
gates each event on "is the author a local user on *this* instance?"
via :func:`AbstractUserRepo.get_instance_for_user`. Remote-author
events flow through the bus untouched here.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ..domain.events import (
    HighlightFrameAdded,
    HighlightFrameReactionChanged,
    HighlightFrameRemoved,
    HighlightFrameViewed,
    HighlightRemoved,
)
from ..domain.federation import FederationEventType
from ..infrastructure.event_bus import EventBus

if TYPE_CHECKING:
    from ..federation.federation_service import FederationService
    from ..repositories.federation_repo import AbstractFederationRepo
    from ..repositories.user_repo import AbstractUserRepo

log = logging.getLogger(__name__)


class HighlightFederationOutbound:
    """Publish Highlight mutations to paired peer instances."""

    __slots__ = ("_bus", "_federation", "_federation_repo", "_user_repo")

    def __init__(
        self,
        *,
        bus: EventBus,
        federation_service: "FederationService",
        federation_repo: "AbstractFederationRepo",
        user_repo: "AbstractUserRepo",
    ) -> None:
        self._bus = bus
        self._federation = federation_service
        self._federation_repo = federation_repo
        self._user_repo = user_repo

    def wire(self) -> None:
        """Subscribe handlers on the bus. Idempotent."""
        self._bus.subscribe(HighlightFrameAdded, self._on_frame_added)
        self._bus.subscribe(HighlightFrameRemoved, self._on_frame_removed)
        self._bus.subscribe(HighlightRemoved, self._on_highlight_removed)
        # Back-channel events flow viewer → author. The echo-loop guard
        # gates on "is the *actor* (viewer / reactor) local?" — same
        # idea as the author-side gate above, just on a different field.
        self._bus.subscribe(HighlightFrameViewed, self._on_frame_viewed)
        self._bus.subscribe(HighlightFrameReactionChanged, self._on_reaction_changed)

    # ── Bus handlers ─────────────────────────────────────────────────────

    async def _on_frame_added(self, event: HighlightFrameAdded) -> None:
        if not await self._is_local_author(event.author_user_id):
            return  # echo from inbound or remote-only — never re-fan
        ev_type = (
            FederationEventType.HIGHLIGHT_CREATED
            if event.is_first_frame
            else FederationEventType.HIGHLIGHT_FRAME_APPENDED
        )
        payload = {
            "highlight_id": event.highlight_id,
            "frame_id": event.frame_id,
            "author_user_id": event.author_user_id,
            "highlight_date": event.highlight_date,
            "sequence": event.sequence,
            "audience_kind": event.audience_kind,
            "audience": list(event.audience),
            "frame_type": event.frame_type,
            "media_url": event.media_url,
            "caption_text": event.caption_text,
            "caption_emoji": event.caption_emoji,
            "duration_ms": event.duration_ms,
            "expires_at": event.expires_at,
            "occurred_at": event.occurred_at.isoformat(),
        }
        await self._fan_to_audience(
            audience_kind=event.audience_kind,
            audience=event.audience,
            event_type=ev_type,
            payload=payload,
        )

    async def _on_frame_removed(self, event: HighlightFrameRemoved) -> None:
        if not await self._is_local_author(event.author_user_id):
            return
        await self._fan_to_audience(
            audience_kind=event.audience_kind,
            audience=event.audience,
            event_type=FederationEventType.HIGHLIGHT_FRAME_DELETED,
            payload={
                "highlight_id": event.highlight_id,
                "frame_id": event.frame_id,
                "author_user_id": event.author_user_id,
                "occurred_at": event.occurred_at.isoformat(),
            },
        )

    async def _on_highlight_removed(self, event: HighlightRemoved) -> None:
        if not await self._is_local_author(event.author_user_id):
            return
        await self._fan_to_audience(
            audience_kind=event.audience_kind,
            audience=event.audience,
            event_type=FederationEventType.HIGHLIGHT_DELETED,
            payload={
                "highlight_id": event.highlight_id,
                "author_user_id": event.author_user_id,
                "occurred_at": event.occurred_at.isoformat(),
            },
        )

    # ── Back-channel: viewer/reactor → author ────────────────────────────

    async def _on_frame_viewed(self, event: HighlightFrameViewed) -> None:
        if not await self._is_local_user(event.viewer_user_id):
            return  # echo from inbound or the author's own view — drop
        # Author-only delivery: the only peer that needs the view
        # receipt is the author's home instance. If the author *is*
        # local (i.e. local viewer + local author), there's no
        # federation work — the local realtime layer already fanned.
        target = await self._home_instance_or_none(event.author_user_id)
        if target is None or target == self._federation.own_instance_id:
            return
        await self._send_to(
            instance_id=target,
            event_type=FederationEventType.HIGHLIGHT_FRAME_VIEWED,
            payload={
                "highlight_id": event.highlight_id,
                "frame_id": event.frame_id,
                "viewer_user_id": event.viewer_user_id,
                "author_user_id": event.author_user_id,
                "occurred_at": event.occurred_at.isoformat(),
            },
        )

    async def _on_reaction_changed(self, event: HighlightFrameReactionChanged) -> None:
        if not await self._is_local_user(event.reactor_user_id):
            return
        target = await self._home_instance_or_none(event.author_user_id)
        if target is None or target == self._federation.own_instance_id:
            return
        if event.emoji is None:
            ev_type = FederationEventType.HIGHLIGHT_FRAME_REACTION_REMOVED
        else:
            ev_type = FederationEventType.HIGHLIGHT_FRAME_REACTED
        await self._send_to(
            instance_id=target,
            event_type=ev_type,
            payload={
                "highlight_id": event.highlight_id,
                "frame_id": event.frame_id,
                "reactor_user_id": event.reactor_user_id,
                "author_user_id": event.author_user_id,
                "emoji": event.emoji,
                "occurred_at": event.occurred_at.isoformat(),
            },
        )

    # ── Helpers ──────────────────────────────────────────────────────────

    async def _is_local_author(self, author_user_id: str) -> bool:
        return await self._is_local_user(author_user_id)

    async def _is_local_user(self, user_id: str) -> bool:
        """Generic 'does this user_id live on *this* instance?' check.

        Used by both the author-side gate (frame added / removed /
        highlight removed) and the actor-side gate (frame viewed / reaction
        changed). Same semantics — different field on the event.
        """
        return (
            await self._home_instance_or_none(user_id)
            == self._federation.own_instance_id
        )

    async def _home_instance_or_none(self, user_id: str) -> str | None:
        try:
            return await self._user_repo.get_instance_for_user(user_id)
        except Exception as exc:  # pragma: no cover — defensive
            log.debug("highlight-outbound: user lookup failed: %s", exc)
            return None

    async def _resolve_audience(
        self,
        kind: str,
        audience: tuple[str, ...],
    ) -> set[str]:
        """Return the set of peer instance_ids to send the event to."""
        own = self._federation.own_instance_id
        if kind == "all_paired":
            try:
                peers = await self._federation_repo.list_instances(
                    status="paired",
                )
            except Exception as exc:  # pragma: no cover — defensive
                log.debug("highlight-outbound: list peers failed: %s", exc)
                return set()
            return {
                pid
                for pid in (getattr(p, "id", None) for p in peers)
                if pid and pid != own
            }
        if kind == "households":
            return {iid for iid in audience if iid and iid != own}
        if kind == "users":
            resolved: set[str] = set()
            for uid in audience:
                try:
                    home = await self._user_repo.get_instance_for_user(uid)
                except Exception as exc:  # pragma: no cover — defensive
                    log.debug(
                        "highlight-outbound: instance lookup for %s failed: %s",
                        uid,
                        exc,
                    )
                    continue
                if home and home != own:
                    resolved.add(home)
            return resolved
        log.debug("highlight-outbound: unknown audience_kind %r", kind)
        return set()

    async def _fan_to_audience(
        self,
        *,
        audience_kind: str,
        audience: tuple[str, ...],
        event_type: FederationEventType,
        payload: dict,
    ) -> None:
        targets = await self._resolve_audience(audience_kind, audience)
        for instance_id in targets:
            await self._send_to(
                instance_id=instance_id,
                event_type=event_type,
                payload=payload,
            )

    async def _send_to(
        self,
        *,
        instance_id: str,
        event_type: FederationEventType,
        payload: dict,
    ) -> None:
        """Single-target wrapper around ``federation_service.send_event``
        that swallows errors so one bad peer doesn't kill the bus
        subscriber. Identical contract to the loop body in
        ``_fan_to_audience`` — extracted so the back-channel handlers
        (which are unicast to the author's home instance) don't have to
        rebuild the audience structure."""
        try:
            await self._federation.send_event(
                to_instance_id=instance_id,
                event_type=event_type,
                payload=payload,
            )
        except Exception as exc:  # pragma: no cover — defensive
            log.debug(
                "highlight-outbound: send %s to %s failed: %s",
                event_type,
                instance_id,
                exc,
            )
