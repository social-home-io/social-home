"""Instance metadata route — what mode are we in, what can the SPA show?

`GET /api/instance/config` is the SPA's first call on cold start. The
response carries the deployment mode, the adapter's capability set, and
the first-boot setup flag so the SPA knows whether to route the user
to `/setup` instead of `/login`.

Public path — no auth required, intentionally. The SPA needs this
*before* it has a token.
"""

from __future__ import annotations

from aiohttp import web

from ..app_keys import (
    config_key,
    platform_adapter_key,
    setup_service_key,
)
from .base import BaseView


class InstanceConfigView(BaseView):
    """``GET /api/instance/config`` — mode + capabilities + setup flag."""

    async def get(self) -> web.Response:
        config = self.svc(config_key)
        adapter = self.svc(platform_adapter_key)
        setup = self.svc(setup_service_key)
        capabilities = sorted(str(c) for c in adapter.capabilities)
        return web.json_response(
            {
                "mode": config.mode,
                "instance_name": config.instance_name,
                "capabilities": capabilities,
                "setup_required": await setup.is_required(),
            }
        )
