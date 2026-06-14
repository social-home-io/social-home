"""Recovery Kit routes — download a passphrase-sealed trust-layer export.

The Recovery Kit lets a household reconstitute the SAME instance_id on
fresh hardware after disk loss (docs/crypto.md "Recovery Kit"). This route
is the build/download side; restore lands with the setup-wizard flow.
Admin-only — it exports the household's crown-jewel key material.
"""

from __future__ import annotations

from aiohttp import web

from .. import app_keys as K
from ..auth import require_admin
from ..services.recovery_kit_service import RecoveryRestoreError
from .base import BaseView


class RecoveryKitExportView(BaseView):
    """POST /api/recovery-kit — build + download the sealed Recovery Kit."""

    async def post(self) -> web.Response:
        require_admin(self.request)
        body = await self.body()
        passphrase = body.get("passphrase")
        if not isinstance(passphrase, str) or len(passphrase) < 8:
            return web.json_response(
                {"error": "passphrase must be at least 8 characters"}, status=422
            )
        svc = self.svc(K.recovery_kit_service_key)
        try:
            blob = await svc.build_kit(passphrase)
        except RecoveryRestoreError as exc:
            return web.json_response({"error": str(exc)}, status=409)
        return web.Response(
            body=blob,
            content_type="application/octet-stream",
            headers={
                "Content-Disposition": (
                    'attachment; filename="socialhome-recovery-kit.shrk"'
                ),
            },
        )
