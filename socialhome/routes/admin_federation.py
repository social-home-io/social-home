"""Admin route for the federation-compatibility panel.

Lists confirmed federation peers with the protocol version each advertises,
the features it lacks versus this build's :data:`OURS`, its last-reachable
timestamp, and whether it has ever advertised capabilities at all
(``capabilities_known`` — a NULL stamp distinguishes a genuine v1 peer from
one that's paired but still mid-first-handshake).

Routes:

* ``GET /api/admin/federation/compat``   (admin-only)
* ``POST /api/admin/federation/resync``  (admin-only) — ask a peer to
  re-broadcast state for a named scope (§319.6).
"""

from __future__ import annotations

from aiohttp import web

from ..app_keys import federation_repo_key, federation_service_key
from ..domain.federation import FederationEventType, PairingStatus
from ..domain.federation_capabilities import (
    OURS,
    FederationCapability,
    features_missing_below,
)
from ..security import error_response
from .base import BaseView


class AdminFederationCompatView(BaseView):
    async def get(self) -> web.Response:
        if self.user is None or not self.user.is_admin:
            return error_response(403, "FORBIDDEN", "Admin only.")
        repo = self.svc(federation_repo_key)
        peers = await repo.list_instances(status=PairingStatus.CONFIRMED.value)
        return self._json(
            {
                "ours": OURS,
                "peers": [
                    {
                        "instance_id": p.id,
                        "display_name": p.effective_display_name,
                        "proto_version": p.proto_version,
                        "status": p.status.value,
                        "last_reachable_at": p.last_reachable_at,
                        "capabilities_known": p.capabilities_seen_at is not None,
                        "lacking_features": features_missing_below(p.proto_version),
                    }
                    for p in peers
                ],
            }
        )


def _valid_scope(scope: str) -> bool:
    """A resync scope is ``capabilities`` or ``space:<id>`` /
    ``calendar:<id>`` with a non-empty id."""
    if scope == "capabilities":
        return True
    for prefix in ("space:", "calendar:"):
        if scope.startswith(prefix):
            return bool(scope[len(prefix) :])
    return False


class AdminFederationResyncView(BaseView):
    """``POST /api/admin/federation/resync`` — ask a peer to re-broadcast.

    Sends :data:`FederationEventType.INSTANCE_RESYNC_REQUEST` to a
    confirmed peer for a named scope (``capabilities`` / ``space:<id>`` /
    ``calendar:<id>``). Gated on the peer advertising
    :data:`FederationCapability.MIN_FOR_INSTANCE_RESYNC` (v_19) — an older
    peer has no handler, so we 409 rather than fire into the void.
    """

    async def post(self) -> web.Response:
        if self.user is None or not self.user.is_admin:
            return error_response(403, "FORBIDDEN", "Admin only.")
        body = await self.body()
        instance_id = str(body.get("instance_id") or "")
        scope = str(body.get("scope") or "")
        if not instance_id or not _valid_scope(scope):
            return error_response(
                400,
                "UNPROCESSABLE",
                "instance_id is required and scope must be 'capabilities', "
                "'space:<id>', or 'calendar:<id>'.",
            )
        fed = self.svc(federation_service_key)
        if not await fed.peer_supports(
            instance_id,
            min_version=FederationCapability.MIN_FOR_INSTANCE_RESYNC,
        ):
            return error_response(
                409,
                "PEER_TOO_OLD",
                "That peer is on an older protocol version and can't honor "
                "a resync request yet.",
            )
        await fed.send_event(
            to_instance_id=instance_id,
            event_type=FederationEventType.INSTANCE_RESYNC_REQUEST,
            payload={"scope": scope},
        )
        return self._json({"status": "ok", "instance_id": instance_id, "scope": scope})
