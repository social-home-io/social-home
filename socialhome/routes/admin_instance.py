"""Admin route to rename the household (the federated instance display name)."""

from __future__ import annotations

from aiohttp import web

from ..app_keys import (
    capabilities_outbound_key,
    federation_repo_key,
    gfs_connection_service_key,
)
from ..security import error_response
from .base import BaseView


class AdminInstanceView(BaseView):
    """``PATCH /api/admin/instance`` — set the household's federated display name.

    Persists ``instance_identity.display_name`` and re-broadcasts it to every
    confirmed peer (via INSTANCE_CAPABILITIES_UPDATED) so paired households see
    the new name without re-pairing.
    """

    async def patch(self) -> web.Response:
        if self.user is None or not self.user.is_admin:
            return error_response(403, "FORBIDDEN", "Admin only.")
        body = await self.body()
        raw = body.get("display_name")
        if not isinstance(raw, str):
            return error_response(422, "UNPROCESSABLE", "display_name is required.")
        name = raw.strip()
        if not name or len(name) > 80:
            return error_response(
                422, "UNPROCESSABLE", "display_name must be 1-80 characters."
            )
        await self.svc(federation_repo_key).set_instance_display_name(name)
        # Best-effort fan-out to confirmed peers; never blocks the rename.
        await self.svc(capabilities_outbound_key).publish()
        # Best-effort: keep every paired GFS's client_instances row in sync.
        # Never raises — an unreachable/old GFS is logged and skipped.
        await self.svc(gfs_connection_service_key).update_display_name_to_all(name)
        return self._json({"display_name": name})
