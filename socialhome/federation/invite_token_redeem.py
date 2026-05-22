"""Cross-instance redeem of a space invite token (§D2).

When a household pastes an invite code minted on a different household,
the SPA POSTs ``/api/spaces/join`` with ``{token, issuer_instance_id}``.
The receiving instance can't validate the token locally — the token row
lives in the *issuer's* ``space_invite_tokens`` table. This coordinator
runs the cross-instance handshake:

1. **Receiver → issuer:** :data:`SPACE_INVITE_TOKEN_REDEEM`
   carrying the token + the redeemer's identity (``user_id``,
   ``public_key``, ``display_name``) + a ``redeem_nonce`` that keys the
   in-flight Future on the receiver side.
2. **Issuer:** atomically consumes the token, seats the redeemer as a
   :class:`SpaceRemoteMember`, registers the receiver's
   ``space_instance``, and ships :data:`SPACE_INVITE_TOKEN_REDEEM_ACK`
   back with ``{redeem_nonce, space_id, role}``.
3. **Receiver:** resolves the Future on the ACK — the route handler
   wakes from its ``await``, persists ``add_space_instance(space_id,
   issuer_instance_id)`` locally, and returns ``{space_id, role}`` to
   the SPA.

On any issuer-side failure (token unknown / expired / exhausted, ban,
crypto error), the issuer sends
:data:`SPACE_INVITE_TOKEN_REDEEM_DENY` with a human-readable
``reason``; the receiver's Future raises
:class:`SpacePermissionError` carrying that reason. On no response
within ``REDEEM_TIMEOUT_SECONDS``, the receiver's Future raises
``TimeoutError`` and the route maps that to HTTP 504.

PR 1: direct-pair only. There is no mesh-relay (``_VIA``) handling
here — the receiver must already have a CONFIRMED ``RemoteInstance``
row for the issuer. PR 2 will layer a relay step on top.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import TYPE_CHECKING

from ..domain.federation import FederationEventType, PairingStatus
from ..domain.space import SpacePermissionError, SpaceRole

if TYPE_CHECKING:
    from ..domain.federation import FederationEvent
    from ..infrastructure.event_bus import EventBus
    from ..repositories.federation_repo import AbstractFederationRepo
    from ..repositories.space_remote_member_repo import (
        AbstractSpaceRemoteMemberRepo,
    )
    from ..repositories.space_repo import AbstractSpaceRepo
    from ..repositories.user_repo import AbstractUserRepo
    from .federation_service import FederationService

log = logging.getLogger(__name__)


#: How long the receiver waits for an ACK / DENY before giving up.
#: Calibrated for a single hop over a healthy WebRTC DataChannel.
REDEEM_TIMEOUT_SECONDS: float = 10.0


class SpaceInviteTokenRedeemCoordinator:
    """Coordinator for the cross-instance ``SPACE_INVITE_TOKEN_REDEEM``
    round-trip.

    The same instance plays both roles in a deployment — receiver when a
    local user pastes a peer's token, issuer when a peer redeems one of
    our tokens. The coordinator hosts both inbound handlers and the
    outbound ``request_redeem`` driver.
    """

    __slots__ = (
        "_bus",
        "_federation",
        "_spaces",
        "_remote_members",
        "_users",
        "_federation_repo",
        "_pending",
        "_timeout",
    )

    def __init__(
        self,
        *,
        bus: "EventBus",
        federation_service: "FederationService",
        space_repo: "AbstractSpaceRepo",
        space_remote_member_repo: "AbstractSpaceRemoteMemberRepo",
        user_repo: "AbstractUserRepo",
        federation_repo: "AbstractFederationRepo",
        timeout: float = REDEEM_TIMEOUT_SECONDS,
    ) -> None:
        self._bus = bus
        self._federation = federation_service
        self._spaces = space_repo
        self._remote_members = space_remote_member_repo
        self._users = user_repo
        self._federation_repo = federation_repo
        #: ``redeem_nonce`` → in-flight Future awaiting the ACK / DENY.
        self._pending: dict[str, asyncio.Future[dict]] = {}
        self._timeout = timeout

    def attach_to(self, federation_service: "FederationService") -> None:
        """Wire the three inbound event-type handlers into the registry."""
        registry = federation_service._event_registry  # noqa: SLF001
        registry.register(
            FederationEventType.SPACE_INVITE_TOKEN_REDEEM,
            self._on_redeem,
        )
        registry.register(
            FederationEventType.SPACE_INVITE_TOKEN_REDEEM_ACK,
            self._on_redeem_ack,
        )
        registry.register(
            FederationEventType.SPACE_INVITE_TOKEN_REDEEM_DENY,
            self._on_redeem_deny,
        )

    # ── Sender side ────────────────────────────────────────────────────

    async def request_redeem(
        self,
        token: str,
        *,
        viewer_user_id: str,
        issuer_instance_id: str,
    ) -> dict:
        """Drive the receiver-side handshake.

        Validates the issuer is a CONFIRMED peer, ships the REDEEM
        envelope, and awaits the ACK / DENY on a nonce-keyed Future.
        Returns ``{space_id, role}`` on ACK. Raises
        :class:`SpacePermissionError` on DENY (or unpaired issuer),
        ``TimeoutError`` on no response within ``self._timeout``.
        """
        instance = await self._federation_repo.get_instance(issuer_instance_id)
        if instance is None or instance.status is not PairingStatus.CONFIRMED:
            raise SpacePermissionError(
                "issuer instance is not a confirmed peer — pair first",
            )

        # Look up the local user so we can ship their identity to the
        # issuer; the issuer needs a public_key + display_name to seat
        # us as a SpaceRemoteMember on its side.
        user = await self._users.get_by_user_id(viewer_user_id)
        if user is None:
            # Surfaces as 403 — a missing local user record on a
            # supposedly-authenticated request is a permission-shape
            # error, not a client validation problem.
            raise SpacePermissionError("local user not found")

        nonce = uuid.uuid4().hex
        loop = asyncio.get_event_loop()
        fut: asyncio.Future[dict] = loop.create_future()
        self._pending[nonce] = fut
        try:
            await self._federation.send_event(
                to_instance_id=issuer_instance_id,
                event_type=FederationEventType.SPACE_INVITE_TOKEN_REDEEM,
                payload={
                    "redeem_nonce": nonce,
                    "invite_token": token,
                    "redeemer_user_id": viewer_user_id,
                    "redeemer_display_name": (user.display_name or user.username),
                    "redeemer_public_key": getattr(user, "public_key", None),
                },
            )
            try:
                result = await asyncio.wait_for(fut, timeout=self._timeout)
            except asyncio.TimeoutError as exc:
                raise TimeoutError(
                    "issuer did not respond to invite-token redeem",
                ) from exc
        finally:
            self._pending.pop(nonce, None)

        # Receiver-side post-condition: register the (space_id, issuer)
        # mapping locally so subsequent space-scoped fan-outs include
        # the issuer's household.
        space_id = str(result.get("space_id") or "")
        if space_id:
            await self._spaces.add_space_instance(
                space_id,
                issuer_instance_id,
            )
        return {
            "space_id": space_id,
            "role": str(result.get("role") or SpaceRole.MEMBER.value),
        }

    # ── Receiver side (the issuer in this exchange) ────────────────────

    async def _on_redeem(self, event: "FederationEvent") -> None:
        """Issuer-side: validate the token, seat the remote redeemer,
        send ACK. On any failure ship a DENY with a ``reason``.
        """
        p = event.payload
        nonce = str(p.get("redeem_nonce") or "")
        token = str(p.get("invite_token") or "")
        if not nonce or not token:
            log.debug(
                "SPACE_INVITE_TOKEN_REDEEM from %s missing nonce/token",
                event.from_instance,
            )
            return  # cannot DENY without a nonce to key the reply

        redeemer_user_id = str(p.get("redeemer_user_id") or "")
        redeemer_pk_raw = p.get("redeemer_public_key")
        redeemer_pk = str(redeemer_pk_raw) if redeemer_pk_raw else None
        redeemer_display_raw = p.get("redeemer_display_name")
        redeemer_display = str(redeemer_display_raw) if redeemer_display_raw else None

        if not redeemer_user_id:
            await self._send_deny(
                event.from_instance,
                nonce,
                "redeemer_user_id missing from redeem payload",
            )
            return

        try:
            row = await self._spaces.consume_invite_token(token)
        except Exception:
            log.exception(
                "SPACE_INVITE_TOKEN_REDEEM: consume_invite_token raised"
                " for token from %s",
                event.from_instance,
            )
            await self._send_deny(
                event.from_instance,
                nonce,
                "issuer storage error during token consume",
            )
            return

        if row is None:
            await self._send_deny(
                event.from_instance,
                nonce,
                "invite token invalid, expired, or exhausted",
            )
            return

        space_id = str(row.get("space_id") or "")
        if not space_id:
            await self._send_deny(
                event.from_instance,
                nonce,
                "invite token row missing space_id",
            )
            return

        # §13.7 — a ban on the issuer side overrides a valid token.
        try:
            banned = await self._spaces.is_banned(space_id, redeemer_user_id)
        except Exception:
            log.exception(
                "SPACE_INVITE_TOKEN_REDEEM: is_banned raised for"
                " space_id=%s user_id=%s",
                space_id,
                redeemer_user_id,
            )
            await self._send_deny(
                event.from_instance,
                nonce,
                "issuer storage error during ban check",
            )
            return
        if banned:
            await self._send_deny(
                event.from_instance,
                nonce,
                "banned from this space",
            )
            return

        # Seat the remote redeemer + register their instance so the
        # issuer's outbound fan-outs reach them.
        try:
            await self._remote_members.add(
                space_id=space_id,
                instance_id=event.from_instance,
                user_id=redeemer_user_id,
                user_pk=redeemer_pk,
                display_name=redeemer_display,
            )
            await self._spaces.add_space_instance(
                space_id,
                event.from_instance,
            )
        except Exception:
            log.exception(
                "SPACE_INVITE_TOKEN_REDEEM: seating remote member failed"
                " for space_id=%s instance=%s user_id=%s",
                space_id,
                event.from_instance,
                redeemer_user_id,
            )
            await self._send_deny(
                event.from_instance,
                nonce,
                "issuer storage error during member seat",
            )
            return

        await self._federation.send_event(
            to_instance_id=event.from_instance,
            event_type=FederationEventType.SPACE_INVITE_TOKEN_REDEEM_ACK,
            payload={
                "redeem_nonce": nonce,
                "space_id": space_id,
                "role": SpaceRole.MEMBER.value,
            },
        )

    async def _on_redeem_ack(self, event: "FederationEvent") -> None:
        """Receiver-side: resolve the in-flight Future with the issuer's
        payload. No-op if the nonce isn't ours (late ACK after timeout).
        """
        p = event.payload
        nonce = str(p.get("redeem_nonce") or "")
        if not nonce:
            return
        fut = self._pending.get(nonce)
        if fut is None or fut.done():
            return
        fut.set_result(
            {
                "space_id": str(p.get("space_id") or ""),
                "role": str(p.get("role") or SpaceRole.MEMBER.value),
            }
        )

    async def _on_redeem_deny(self, event: "FederationEvent") -> None:
        """Receiver-side: resolve the in-flight Future with a
        :class:`SpacePermissionError` carrying the issuer's reason.
        """
        p = event.payload
        nonce = str(p.get("redeem_nonce") or "")
        if not nonce:
            return
        fut = self._pending.get(nonce)
        if fut is None or fut.done():
            return
        reason = str(p.get("reason") or "invite redeem denied by issuer")
        fut.set_exception(SpacePermissionError(reason))

    # ── Internal helpers ───────────────────────────────────────────────

    async def _send_deny(
        self,
        to_instance_id: str,
        nonce: str,
        reason: str,
    ) -> None:
        """Best-effort DENY ship-back. Logged but never raised — a
        failed DENY just leaves the receiver hanging until its timeout,
        which is the same outcome as the network dropping the frame.
        """
        try:
            await self._federation.send_event(
                to_instance_id=to_instance_id,
                event_type=(FederationEventType.SPACE_INVITE_TOKEN_REDEEM_DENY),
                payload={
                    "redeem_nonce": nonce,
                    "reason": reason,
                },
            )
        except Exception:
            log.exception(
                "SPACE_INVITE_TOKEN_REDEEM_DENY ship-back to %s failed",
                to_instance_id,
            )
