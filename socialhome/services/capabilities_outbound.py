"""Outbound federation for ``INSTANCE_CAPABILITIES_UPDATED``.

Fans out this build's :data:`~socialhome.domain.federation_capabilities.OURS`
``proto_version`` to every confirmed peer so they can gate optional
outbound fields on the version we actually run. Called at startup;
re-invoke any time the advertised version changes mid-run (rare —
typically only on restart after a release).

Mirrors :class:`UrlUpdateOutbound` in shape so the
"fan-out-on-config-change" pattern stays consistent. A failed send to
a single peer lands in the outbox retry queue; we never raise.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ..domain.federation import FederationEventType
from ..domain.federation_capabilities import OURS as OUR_PROTO_VERSION

if TYPE_CHECKING:
    from ..federation.federation_service import FederationService
    from ..repositories.federation_repo import AbstractFederationRepo

log = logging.getLogger(__name__)


class CapabilitiesOutbound:
    """Fan out ``INSTANCE_CAPABILITIES_UPDATED`` to every confirmed peer."""

    __slots__ = ("_federation", "_federation_repo")

    def __init__(
        self,
        *,
        federation_service: "FederationService",
        federation_repo: "AbstractFederationRepo",
    ) -> None:
        self._federation = federation_service
        self._federation_repo = federation_repo

    async def publish(self) -> int:
        """Tell every confirmed peer our current ``proto_version``.

        Returns the number of peers successfully notified (best-effort
        — a failed send to one peer doesn't block the others; the
        outbox retry layer redelivers anything that landed in the
        queue).
        """
        try:
            peers = await self._federation_repo.list_instances(status="confirmed")
        except Exception as exc:  # pragma: no cover — defensive
            log.debug("capabilities-outbound: list peers failed: %s", exc)
            return 0
        own = getattr(self._federation, "_own_instance_id", "")
        payload = {"proto_version": OUR_PROTO_VERSION}
        sent = 0
        for peer in peers:
            instance_id = getattr(peer, "id", None)
            if not instance_id or instance_id == own:
                continue
            try:
                await self._federation.send_event(
                    to_instance_id=instance_id,
                    event_type=FederationEventType.INSTANCE_CAPABILITIES_UPDATED,
                    payload=payload,
                )
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
