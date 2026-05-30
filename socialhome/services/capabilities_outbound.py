"""Outbound federation for ``INSTANCE_CAPABILITIES_UPDATED``.

Fans out this build's :data:`~socialhome.domain.federation_capabilities.OURS`
``proto_version`` to every confirmed peer so they can gate optional
outbound fields on the version we actually run. Three trigger points:

1. **At app startup** — :meth:`publish` fans out to every peer that's
   confirmed *at boot time*. Covers the steady-state case.
2. **When a new pair lands** — :meth:`wire` subscribes to
   :class:`PairingConfirmed` so the freshly-confirmed peer gets a
   targeted announcement immediately, without waiting for the next
   restart. Without this, a peer paired *after* startup never learns
   our version and outbound senders that gate on ``peer_supports``
   would skip optional fields forever.
3. **Manually** — call :meth:`publish` after the operator changes the
   advertised set mid-run (rare; usually only on restart).

A failed send to a single peer lands in the outbox retry queue; we
never raise.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ..domain.events import PairingConfirmed
from ..domain.federation import FederationEventType
from ..domain.federation_capabilities import OURS as OUR_PROTO_VERSION
from .peer_outbound import ConfirmedPeerBroadcaster

if TYPE_CHECKING:
    from ..federation.federation_service import FederationService
    from ..infrastructure.event_bus import EventBus
    from ..repositories.federation_repo import AbstractFederationRepo

log = logging.getLogger(__name__)


class CapabilitiesOutbound(ConfirmedPeerBroadcaster):
    """Fan out ``INSTANCE_CAPABILITIES_UPDATED`` to every confirmed peer."""

    __slots__ = ("_federation", "_federation_repo", "_bus")

    # Narrow the broadcaster's optional ``_federation`` — required at
    # construction, so the direct ``send_event`` access below is non-None.
    _federation: "FederationService"

    def __init__(
        self,
        *,
        federation_service: "FederationService",
        federation_repo: "AbstractFederationRepo",
        bus: "EventBus | None" = None,
    ) -> None:
        self._federation = federation_service
        self._federation_repo = federation_repo
        self._bus = bus

    def wire(self) -> None:
        """Subscribe to :class:`PairingConfirmed` so new pairs get a
        targeted announcement as soon as the handshake completes.

        Idempotent — the bus dedupes subscribers per (event_type, fn).
        Call once during app wiring after the bus is built.
        """
        if self._bus is None:
            return
        self._bus.subscribe(PairingConfirmed, self._on_pairing_confirmed)

    async def _on_pairing_confirmed(self, event: PairingConfirmed) -> None:
        """A new peer just became CONFIRMED — send our version to them.

        Targeted send (not a fan-out), so the cost is one envelope per
        pair regardless of how many peers we have. Fails-soft on send
        errors — the outbox retry layer redelivers, and the periodic
        startup fan-out covers any pair that drops to UNCONFIRMED and
        is later re-confirmed.
        """
        try:
            await self._send_to(event.instance_id)
        except Exception as exc:  # pragma: no cover — defensive
            log.debug(
                "capabilities-outbound: on-pair send to %s failed: %s",
                event.instance_id,
                exc,
            )

    async def _send_to(self, instance_id: str) -> bool:
        own = getattr(self._federation, "_own_instance_id", "")
        if not instance_id or instance_id == own:
            return False
        await self._federation.send_event(
            to_instance_id=instance_id,
            event_type=FederationEventType.INSTANCE_CAPABILITIES_UPDATED,
            payload={"proto_version": OUR_PROTO_VERSION},
        )
        log.info(
            "capabilities-outbound: sent proto_version=%d to %s",
            OUR_PROTO_VERSION,
            instance_id,
        )
        return True

    async def publish(self) -> int:
        """Tell every confirmed peer our current ``proto_version``.

        Returns the number of peers successfully notified (best-effort
        — a failed send to one peer doesn't block the others; the
        outbox retry layer redelivers anything that landed in the
        queue).
        """
        sent = 0
        for instance_id in await self.list_confirmed_peer_ids():
            try:
                if await self._send_to(instance_id):
                    sent += 1
            except Exception as exc:  # pragma: no cover — defensive
                log.debug(
                    "capabilities-outbound: send to %s failed: %s",
                    instance_id,
                    exc,
                )
        log.info(
            "capabilities-outbound: notified %d peer(s), proto_version=%d",
            sent,
            OUR_PROTO_VERSION,
        )
        return sent
