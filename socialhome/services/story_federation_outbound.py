"""Outbound federation for personal Stories (§Stories).

Subscribes to :class:`StoryFrameAdded`, :class:`StoryFrameRemoved` and
:class:`StoryRemoved` and translates the local-write events into the
matching ``STORY_*`` federation envelopes targeted at the audience.

Audience routing — Stories are *not* space-scoped:

* ``audience_kind = 'all_paired'`` → fan to every confirmed peer.
* ``audience_kind = 'households'`` → fan to the listed peer instance ids
  only (already in instance-id form on the event payload).
* ``audience_kind = 'users'``      → resolve each ``audience`` user id
  to its home instance via :class:`AbstractUserRepo` and dedup.

**Echo-loop guard.** :class:`FederationInboundService` republishes
``StoryFrameAdded`` / ``StoryRemoved`` / ``StoryFrameRemoved`` on the
local bus so :class:`RealtimeService` can fan to local viewers. To
avoid re-fanning that event back to every peer, this subscriber
gates each event on "is the author a local user on *this* instance?"
via :func:`AbstractUserRepo.get_instance_for_user`. Remote-author
events flow through the bus untouched here.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ..domain.events import StoryFrameAdded, StoryFrameRemoved, StoryRemoved
from ..domain.federation import FederationEventType
from ..infrastructure.event_bus import EventBus

if TYPE_CHECKING:
    from ..federation.federation_service import FederationService
    from ..repositories.federation_repo import AbstractFederationRepo
    from ..repositories.user_repo import AbstractUserRepo

log = logging.getLogger(__name__)


class StoryFederationOutbound:
    """Publish Story mutations to paired peer instances."""

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
        self._bus.subscribe(StoryFrameAdded, self._on_frame_added)
        self._bus.subscribe(StoryFrameRemoved, self._on_frame_removed)
        self._bus.subscribe(StoryRemoved, self._on_story_removed)

    # ── Bus handlers ─────────────────────────────────────────────────────

    async def _on_frame_added(self, event: StoryFrameAdded) -> None:
        if not await self._is_local_author(event.author_user_id):
            return  # echo from inbound or remote-only — never re-fan
        ev_type = (
            FederationEventType.STORY_CREATED
            if event.is_first_frame
            else FederationEventType.STORY_FRAME_APPENDED
        )
        payload = {
            "story_id": event.story_id,
            "frame_id": event.frame_id,
            "author_user_id": event.author_user_id,
            "story_date": event.story_date,
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

    async def _on_frame_removed(self, event: StoryFrameRemoved) -> None:
        if not await self._is_local_author(event.author_user_id):
            return
        await self._fan_to_audience(
            audience_kind=event.audience_kind,
            audience=event.audience,
            event_type=FederationEventType.STORY_FRAME_DELETED,
            payload={
                "story_id": event.story_id,
                "frame_id": event.frame_id,
                "author_user_id": event.author_user_id,
                "occurred_at": event.occurred_at.isoformat(),
            },
        )

    async def _on_story_removed(self, event: StoryRemoved) -> None:
        if not await self._is_local_author(event.author_user_id):
            return
        await self._fan_to_audience(
            audience_kind=event.audience_kind,
            audience=event.audience,
            event_type=FederationEventType.STORY_DELETED,
            payload={
                "story_id": event.story_id,
                "author_user_id": event.author_user_id,
                "occurred_at": event.occurred_at.isoformat(),
            },
        )

    # ── Helpers ──────────────────────────────────────────────────────────

    async def _is_local_author(self, author_user_id: str) -> bool:
        try:
            home = await self._user_repo.get_instance_for_user(author_user_id)
        except Exception as exc:  # pragma: no cover — defensive
            log.debug("story-outbound: user lookup failed: %s", exc)
            return False
        own = self._federation.own_instance_id
        return bool(home) and home == own

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
                log.debug("story-outbound: list peers failed: %s", exc)
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
                        "story-outbound: instance lookup for %s failed: %s",
                        uid,
                        exc,
                    )
                    continue
                if home and home != own:
                    resolved.add(home)
            return resolved
        log.debug("story-outbound: unknown audience_kind %r", kind)
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
            try:
                await self._federation.send_event(
                    to_instance_id=instance_id,
                    event_type=event_type,
                    payload=payload,
                )
            except Exception as exc:  # pragma: no cover — defensive
                log.debug(
                    "story-outbound: send %s to %s failed: %s",
                    event_type,
                    instance_id,
                    exc,
                )
