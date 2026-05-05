"""Outbound federation for the Momentum pillar (§Momentum).

Subscribes to :class:`MomentCreated`, :class:`MomentDeleted`,
:class:`MomentReactionChanged` on the bus and translates each into
the matching ``MOMENT_*`` envelope. Two distinct fan-out paths share
the code in this file:

1. **Origin fan-out.** The author's instance fans the moment to every
   confirmed peer with ``hop_count = 1`` and
   ``origin_instance_id = self``.

2. **Relay fan-out** (up to 3 hops total). When an inbound
   ``MOMENT_*`` envelope lands on an instance and ``hop_count < 3``,
   :meth:`relay_inbound` re-fans the same payload to the local paired
   peers, excluding the origin instance and the immediate sender.
   Receivers dedupe by ``moment.id`` (the row's PRIMARY KEY makes the
   second save a no-op).

**Echo-loop guard.** Bus events fired by inbound handlers are
indistinguishable from local writes by event-class identity alone, so
the subscriber gates each event on "is the *actor* (author for create
/ delete, reactor for reactions) local on this instance?" — only the
local actor's instance fans on the bus path. The relay path is
explicit (not bus-driven) and runs from inside the inbound handler
before it republishes anything.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ..domain.events import (
    MomentCreated,
    MomentDeleted,
    MomentReactionChanged,
)
from ..domain.federation import FederationEventType
from ..domain.moment import MOMENT_MAX_HOPS
from ..infrastructure.event_bus import EventBus

if TYPE_CHECKING:
    from ..federation.federation_service import FederationService
    from ..repositories.federation_repo import AbstractFederationRepo
    from ..repositories.user_repo import AbstractUserRepo

log = logging.getLogger(__name__)


class MomentFederationOutbound:
    """Publish moment mutations to the 3-hop peer mesh."""

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
        self._bus.subscribe(MomentCreated, self._on_created)
        self._bus.subscribe(MomentDeleted, self._on_deleted)
        self._bus.subscribe(MomentReactionChanged, self._on_reaction_changed)

    # ── Bus subscribers (origin-side fan-out) ──────────────────────────

    async def _on_created(self, event: MomentCreated) -> None:
        if not await self._is_local_user(event.author_user_id):
            return
        await self._fan_to_peers(
            event_type=FederationEventType.MOMENT_CREATED,
            payload={
                "moment_id": event.moment_id,
                "author_user_id": event.author_user_id,
                "content": event.content,
                "media_url": event.media_url,
                "media_type": event.media_type,
                "duration_ms": event.duration_ms,
                "parent_moment_id": event.parent_moment_id,
                "origin_instance_id": event.origin_instance_id,
                "expires_at": event.expires_at,
                "occurred_at": event.occurred_at.isoformat(),
                "hop_count": 1,
            },
            origin_instance_id=event.origin_instance_id,
            exclude_instances=set(),
        )

    async def _on_deleted(self, event: MomentDeleted) -> None:
        if not await self._is_local_user(event.author_user_id):
            return
        await self._fan_to_peers(
            event_type=FederationEventType.MOMENT_DELETED,
            payload={
                "moment_id": event.moment_id,
                "author_user_id": event.author_user_id,
                "origin_instance_id": event.origin_instance_id,
                "occurred_at": event.occurred_at.isoformat(),
                "hop_count": 1,
            },
            origin_instance_id=event.origin_instance_id,
            exclude_instances=set(),
        )

    async def _on_reaction_changed(self, event: MomentReactionChanged) -> None:
        if not await self._is_local_user(event.reactor_user_id):
            return
        ev_type = (
            FederationEventType.MOMENT_REACTION_REMOVED
            if event.emoji is None
            else FederationEventType.MOMENT_REACTED
        )
        # Reactions are unicast to the author's home instance —
        # everyone else's view of the reaction is hydrated from the
        # author's instance via the next list refresh. (Same shape as
        # the Stories back-channel.)
        target = await self._home_or_none(event.author_user_id)
        if target is None or target == self._federation.own_instance_id:
            return
        await self._send_to(
            instance_id=target,
            event_type=ev_type,
            payload={
                "moment_id": event.moment_id,
                "reactor_user_id": event.reactor_user_id,
                "author_user_id": event.author_user_id,
                "emoji": event.emoji,
                "occurred_at": event.occurred_at.isoformat(),
            },
        )

    # ── Relay (called by the inbound handler when hop_count < 3) ───────

    async def relay_inbound(
        self,
        *,
        event_type: FederationEventType,
        payload: dict,
        from_instance: str,
    ) -> None:
        """Re-broadcast an inbound moment envelope to *our* paired peers,
        bumping ``hop_count`` and excluding both the original origin
        and the immediate sender. No-op when the payload already hit
        ``MOMENT_MAX_HOPS``.
        """
        try:
            hop = int(payload.get("hop_count") or 0)
        except TypeError, ValueError:
            hop = 0
        if hop <= 0 or hop >= MOMENT_MAX_HOPS:
            return
        origin = str(payload.get("origin_instance_id") or "")
        next_payload = dict(payload)
        next_payload["hop_count"] = hop + 1
        await self._fan_to_peers(
            event_type=event_type,
            payload=next_payload,
            origin_instance_id=origin,
            exclude_instances={from_instance},
        )

    # ── Helpers ────────────────────────────────────────────────────────

    async def _fan_to_peers(
        self,
        *,
        event_type: FederationEventType,
        payload: dict,
        origin_instance_id: str,
        exclude_instances: set[str],
    ) -> None:
        own = self._federation.own_instance_id
        try:
            peers = await self._federation_repo.list_instances(status="paired")
        except Exception as exc:  # pragma: no cover — defensive
            log.debug("moment-outbound: list peers failed: %s", exc)
            return
        skip = exclude_instances | {own, origin_instance_id}
        for peer in peers:
            instance_id = getattr(peer, "id", None)
            if not instance_id or instance_id in skip:
                continue
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
        try:
            await self._federation.send_event(
                to_instance_id=instance_id,
                event_type=event_type,
                payload=payload,
            )
        except Exception as exc:  # pragma: no cover — defensive
            log.debug(
                "moment-outbound: send %s to %s failed: %s",
                event_type,
                instance_id,
                exc,
            )

    async def _is_local_user(self, user_id: str) -> bool:
        return await self._home_or_none(user_id) == self._federation.own_instance_id

    async def _home_or_none(self, user_id: str) -> str | None:
        try:
            return await self._user_repo.get_instance_for_user(user_id)
        except Exception as exc:  # pragma: no cover — defensive
            log.debug("moment-outbound: user lookup failed: %s", exc)
            return None
