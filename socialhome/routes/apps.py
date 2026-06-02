"""App-registry routes — install, uninstall, enable/disable, and catalog.

Three resource groups:

* ``AppCollectionView`` — ``GET /api/apps`` (list) + ``POST /api/apps`` (install)
* ``AppCatalogView``    — ``GET /api/apps/catalog`` (browse remote catalog)
* ``AppDetailView``     — ``GET /api/apps/{app_id}`` + ``PATCH`` (toggle) + ``DELETE``
"""

from __future__ import annotations

from aiohttp import web

from ..app_keys import app_service_key
from ..domain.apps import InstalledApp
from ..security import error_response
from .base import BaseView


def _serialize(app: InstalledApp) -> dict:
    """Return a safe, wire-shape dict for an :class:`InstalledApp`."""
    return {
        "app_id": app.app_id,
        "name": app.name,
        "version": app.version,
        "enabled": app.enabled,
        "capabilities": list(app.manifest.capabilities),
        "icon": app.manifest.icon,
    }


class AppCollectionView(BaseView):
    """``GET /api/apps`` + ``POST /api/apps``."""

    async def get(self) -> web.Response:
        user = self.user
        svc = self.svc(app_service_key)
        apps = await svc.list_installed()
        if not user.is_admin:
            apps = [a for a in apps if a.enabled]
        return self._json({"apps": [_serialize(a) for a in apps]})

    async def post(self) -> web.Response:
        user = self.user
        if not user.is_admin:
            return error_response(403, "FORBIDDEN", "Admin only.")

        body = await self.body()
        app_id = body.get("app_id")
        if not isinstance(app_id, str) or not app_id:
            return error_response(
                400, "UNPROCESSABLE", "app_id must be a non-empty string."
            )

        svc = self.svc(app_service_key)
        app = await svc.install(
            app_id,
            actor_is_admin=True,
            actor_user_id=user.user_id,
        )
        return self._json(_serialize(app), status=201)


class AppCatalogView(BaseView):
    """``GET /api/apps/catalog``."""

    async def get(self) -> web.Response:
        user = self.user
        if not user.is_admin:
            return error_response(403, "FORBIDDEN", "Admin only.")

        svc = self.svc(app_service_key)
        entries = await svc.browse_catalog()
        return self._json(
            {
                "apps": [
                    {
                        "app_id": e.app_id,
                        "name": e.name,
                        "latest_version": e.latest_version,
                        "description": e.description,
                        "icon_url": e.icon_url,
                        "capabilities": list(e.capabilities),
                    }
                    for e in entries
                ]
            }
        )


class AppDetailView(BaseView):
    """``GET /api/apps/{app_id}`` + ``PATCH`` + ``DELETE``."""

    async def get(self) -> web.Response:
        user = self.user
        app_id = self.match("app_id")
        svc = self.svc(app_service_key)
        app = await svc.get(app_id)
        if app is None or (not user.is_admin and not app.enabled):
            return error_response(404, "NOT_FOUND", "App not found.")
        return self._json(_serialize(app))

    async def patch(self) -> web.Response:
        user = self.user
        if not user.is_admin:
            return error_response(403, "FORBIDDEN", "Admin only.")

        app_id = self.match("app_id")
        body = await self.body()
        if "enabled" not in body:
            return error_response(400, "UNPROCESSABLE", "enabled field is required.")
        enabled = body["enabled"]
        if not isinstance(enabled, bool):
            return error_response(400, "UNPROCESSABLE", "enabled must be a boolean.")

        svc = self.svc(app_service_key)
        app = await svc.set_enabled(app_id, enabled=enabled, actor_is_admin=True)
        return self._json(_serialize(app))

    async def delete(self) -> web.Response:
        user = self.user
        if not user.is_admin:
            return error_response(403, "FORBIDDEN", "Admin only.")

        app_id = self.match("app_id")
        svc = self.svc(app_service_key)
        await svc.uninstall(app_id, actor_is_admin=True)
        return self._json({"status": "ok"})
