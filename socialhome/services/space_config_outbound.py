"""Outbound federation bridge for space-config changes.

Subscribes to :class:`SpaceConfigChanged` and broadcasts
:data:`FederationEventType.SPACE_CONFIG_CHANGED` to every household
that has a member in the space, so remote-stub holders apply the
rename / emoji / feature toggle / location_mode flip in realtime
(matching the inbound handler at
:meth:`FederationInboundService._on_space_config_changed`).

Before this service the only path that shipped ``SPACE_CONFIG_CHANGED``
was the §D1b catch-up reply (``_push_config_to`` triggered by an
incoming ``SPACE_CONFIG_CATCH_UP``). Steady-state edits never reached
remote stubs — a host who toggled ``location_mode`` from ``zone_only``
to ``gps`` left every remote member's stub stuck on the prior mode,
which silently broke the space map (the API's strict
``loc.mode == mode_filter`` filter dropped pin rows the receiver had
otherwise validated + persisted).

The payload mirrors :func:`_push_config_to`'s shape: flat legacy
fields **plus** ``space_meta`` (the same blob
:func:`stub_space_from_metadata` consumes everywhere else), so the
inbound handler can apply it without conditional shape detection.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ..domain.events import CpSpaceAgeGateChanged, SpaceConfigChanged
from ..domain.federation import FederationEventType
from ..domain.federation_capabilities import FederationCapability
from ..domain.space import SpaceConfigEventType
from ..infrastructure.event_bus import EventBus
from .space_crypto_service import sign_authority_event, strip_authority_sig_fields
from .space_service import _space_metadata_for_federation

if TYPE_CHECKING:
    from ..federation.federation_service import FederationService
    from ..repositories.space_repo import AbstractSpaceRepo

log = logging.getLogger(__name__)

#: Roster / moderation events that ride the LOCAL bus (for realtime/UI) but
#: must NOT federate as ``SPACE_CONFIG_CHANGED``. Their roster effect federates
#: via authority-signed roster gossip (``SPACE_MEMBER_JOINED`` / ``LEFT``) or
#: is host-local only (unban clears a host-side ban flag the stubs never held).
#: Federating them as config — now that they no longer advance
#: ``config_sequence`` — would at best be a no-op and at worst perturb a
#: receiver's ``config_author`` at an equal sequence (a non-deterministic LWW
#: tie-break). So skip the federation outbound for them.
_ROSTER_EVENT_TYPES = {
    SpaceConfigEventType.ADMIN_GRANTED.value,
    SpaceConfigEventType.ADMIN_REVOKED.value,
    SpaceConfigEventType.MEMBER_BANNED.value,
    SpaceConfigEventType.MEMBER_UNBANNED.value,
}


class SpaceConfigOutbound:
    """Bus-event → federation broadcaster for space config changes."""

    __slots__ = ("_bus", "_federation", "_space_repo")

    def __init__(
        self,
        *,
        bus: EventBus,
        federation_service: "FederationService",
        space_repo: "AbstractSpaceRepo",
    ) -> None:
        self._bus = bus
        self._federation = federation_service
        self._space_repo = space_repo

    def wire(self) -> None:
        self._bus.subscribe(SpaceConfigChanged, self._on_config_changed)
        self._bus.subscribe(CpSpaceAgeGateChanged, self._on_age_gate_changed)

    async def _on_age_gate_changed(self, event: CpSpaceAgeGateChanged) -> None:
        """§CP.F1 — federate an age-gate change to member households so
        their stubs stay current (the inbound counterpart is
        ``space_membership._on_age_gate``). Host-only: a stub on a
        non-host household has no authority to push the gate around.

        Join-time propagation rides in ``space_meta`` (see
        ``_space_metadata_for_federation``); this covers the case where the
        host changes the gate *after* members already hold a stub.
        """
        space = await self._space_repo.get(event.space_id)
        if space is None:
            return
        own = getattr(self._federation, "_own_instance_id", "") or ""
        if space.owner_instance_id != own:
            return
        try:
            await self._federation.broadcast_to_space_members(
                space.id,
                FederationEventType.SPACE_AGE_GATE_UPDATED,
                {
                    "space_id": space.id,
                    "min_age": event.min_age,
                    "target_audience": event.target_audience,
                },
            )
        except Exception:
            log.exception(
                "SPACE_AGE_GATE_UPDATED broadcast failed for space=%s",
                space.id,
            )

    async def _on_config_changed(self, event: SpaceConfigChanged) -> None:
        # The bus event carries the changed-fields ``payload`` dict;
        # the full snapshot (every column remote stubs need) requires
        # a re-read of the row. Use the post-mutation row so the
        # broadcast reflects the latest sequence + every field.
        space = await self._space_repo.get(event.space_id)
        if space is None:
            log.debug(
                "SpaceConfigChanged for unknown space %s — skipping",
                event.space_id,
            )
            return
        own = getattr(self._federation, "_own_instance_id", "") or ""
        # Dissolution is a removal, not a config edit. ``SpaceService.
        # dissolve_space`` broadcasts the dedicated SPACE_DISSOLVED itself
        # (so propagation doesn't depend on this subscriber being wired),
        # so just skip here — emitting SPACE_CONFIG_CHANGED would instead
        # refresh members' stubs and resurrect a row the purge removed.
        if event.event_type == SpaceConfigEventType.DISSOLVED.value:
            return
        # Roster / moderation events federate via roster gossip (or are
        # host-local), never as a config edit — see _ROSTER_EVENT_TYPES.
        if event.event_type in _ROSTER_EVENT_TYPES:
            return
        # Who is authorised to broadcast this config edit?
        #
        #   * A seed-holder (owner host OR a delegated admin who received the
        #     space signing seed via SPACE_ADMIN_KEY_SHARE) signs the edit with
        #     the space's Ed25519 seed — every member household (and the offline
        #     owner on reconnect) accepts it by verifying the signature against
        #     ``spaces.identity_public_key``, NOT by trusting ``from_instance``
        #     (v_24). This is what lets a delegated admin change config while the
        #     owner is offline. The signed broadcast is gated on
        #     ``MIN_FOR_ADMIN_AUTHORITATIVE_OPS``; a sub-v_24 member can't verify
        #     it and falls back to the owner-only gate (so a non-owner's signed
        #     edit is dropped there until the owner re-broadcasts / sync repairs).
        #   * The OWNER host with NO stored seed (pre-Phase-0 owned space) still
        #     broadcasts, UNSIGNED — back-compat. A sub-v_24 peer accepts it via
        #     the legacy ``from_instance == owner`` path.
        #   * Anyone else (a member stub holder who is neither owner nor a
        #     seed-holder) has no authority to push config around — skip.
        seed: bytes | None = None
        try:
            seed = await self._space_repo.get_space_seed(space.id)
        except Exception:
            log.exception(
                "SPACE_CONFIG_CHANGED: get_space_seed failed for space=%s",
                space.id,
            )
            seed = None
        is_owner = bool(own) and space.owner_instance_id == own
        if seed is None and not is_owner:
            return
        meta = _space_metadata_for_federation(space)
        min_proto_version: int | None = None
        if seed is not None:
            # Record THIS household as the author of the edit (the v_24 LWW
            # tie-break key) inside the signed bytes, then sign over the bare
            # meta. ``strip_authority_sig_fields`` is used identically on the
            # verify side so the canonical signing bytes match.
            meta["config_author_instance"] = own
            signed = sign_authority_event(
                event_type="space_config_changed",
                space_id=space.id,
                payload=strip_authority_sig_fields(meta),
                space_seed=seed,
            )
            meta.update(signed)
            min_proto_version = FederationCapability.MIN_FOR_ADMIN_AUTHORITATIVE_OPS
        payload: dict = {
            "space_id": space.id,
            "sequence": space.config_sequence,
            "event_type": event.event_type,
            # Flat legacy shape kept for back-compat with pre-§D1b peers
            # that read these fields directly.
            "name": space.name,
            "description": space.description,
            "emoji": space.emoji,
            "join_mode": space.join_mode.value,
            "space_type": space.space_type.value,
            "features": space.features.to_wire_dict(),
            "retention_days": space.retention_days,
            # Modern shape — what stub_space_from_metadata consumes.
            "space_meta": meta,
        }
        try:
            if min_proto_version is not None:
                await self._federation.broadcast_to_space_members(
                    space.id,
                    FederationEventType.SPACE_CONFIG_CHANGED,
                    payload,
                    min_proto_version=min_proto_version,
                )
            else:
                await self._federation.broadcast_to_space_members(
                    space.id,
                    FederationEventType.SPACE_CONFIG_CHANGED,
                    payload,
                )
        except Exception:
            log.exception(
                "SPACE_CONFIG_CHANGED broadcast failed for space=%s",
                space.id,
            )
