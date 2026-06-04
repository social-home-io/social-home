"""Admin route for the federation-compatibility panel.

Lists confirmed federation peers with the protocol version each advertises,
the features it lacks versus this build's :data:`OURS`, its last-reachable
timestamp, and whether it has ever advertised capabilities at all
(``capabilities_known`` — a NULL stamp distinguishes a genuine v1 peer from
one that's paired but still mid-first-handshake).

Routes:

* ``GET /api/admin/federation/compat``  (admin-only)
"""

from __future__ import annotations

from aiohttp import web

from ..app_keys import federation_repo_key
from ..domain.federation import PairingStatus
from ..domain.federation_capabilities import OURS, features_missing_below
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
