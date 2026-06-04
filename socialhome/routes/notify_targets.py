"""Notify-targets route — ``GET /api/me/notify-targets``."""

from __future__ import annotations

from aiohttp import web

from ..app_keys import platform_adapter_key
from .base import BaseView


class NotifyTargetsView(BaseView):
    """``GET /api/me/notify-targets`` — selectable push notify targets.

    Returns the platform's user-selectable notify targets for the profile
    notification-settings dropdown: HA mode lists the household's
    ``notify.*`` entities; platforms with no selectable targets return an
    empty list. Auth is enforced by middleware (no explicit check needed).
    """

    async def get(self) -> web.Response:
        adapter = self.svc(platform_adapter_key)
        push = adapter.push
        targets = await push.list_notify_targets() if push is not None else []
        return self._json({"targets": targets})
