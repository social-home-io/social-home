"""App-registry routes — install, uninstall, enable/disable, catalog, and per-user store.

Four resource groups:

* ``AppCollectionView``      — ``GET /api/apps`` (list) + ``POST /api/apps`` (install)
* ``AppCatalogView``         — ``GET /api/apps/catalog`` (browse remote catalog)
* ``AppDetailView``          — ``GET /api/apps/{app_id}`` + ``PATCH`` (toggle) + ``DELETE``
* ``AppStoreCollectionView`` — ``GET /api/apps/{app_id}/store`` (list all KV pairs)
* ``AppStoreItemView``       — ``GET/PUT/DELETE /api/apps/{app_id}/store/{key}``
"""

from __future__ import annotations

import json

from aiohttp import web

from ..app_keys import app_federation_service_key, app_service_key
from ..domain.apps import InstalledApp
from ..security import error_response
from .base import BaseView

# Maximum encoded payload size for app federation messages (256 KiB).
_MAX_APP_PAYLOAD_BYTES = 256 * 1024


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


class AppStoreCollectionView(BaseView):
    """``GET /api/apps/{app_id}/store`` — list all KV pairs for the caller."""

    async def get(self) -> web.Response:
        app_id = self.match("app_id")
        svc = self.svc(app_service_key)
        items = await svc.store_list(app_id, self.user.user_id)
        # Do NOT use self._json here: sanitise_for_api strips keys in
        # SENSITIVE_FIELDS (e.g. "signature", "endpoint") from nested dicts,
        # which would silently corrupt opaque user-supplied KV values.
        return web.json_response({"items": items})


class AppStoreItemView(BaseView):
    """``GET/PUT/DELETE /api/apps/{app_id}/store/{key}`` — single KV entry."""

    async def get(self) -> web.Response:
        app_id, key = self.match("app_id"), self.match("key")
        svc = self.svc(app_service_key)
        try:
            value = await svc.store_get(app_id, self.user.user_id, key)
        except KeyError:
            return error_response(404, "NOT_FOUND", "Key not found.")
        # Do NOT use self._json here: sanitise_for_api strips keys in
        # SENSITIVE_FIELDS (e.g. "signature", "endpoint") from nested dicts,
        # which would silently corrupt opaque user-supplied KV values.
        return web.json_response({"key": key, "value": value})

    async def put(self) -> web.Response:
        app_id, key = self.match("app_id"), self.match("key")
        body = await self.body()
        if "value" not in body:
            return error_response(400, "UNPROCESSABLE", "value is required.")
        svc = self.svc(app_service_key)
        await svc.store_set(app_id, self.user.user_id, key, body["value"])
        # Do NOT use self._json here: sanitise_for_api strips keys in
        # SENSITIVE_FIELDS (e.g. "signature", "endpoint") from nested dicts,
        # which would silently corrupt opaque user-supplied KV values.
        return web.json_response({"key": key, "value": body["value"]})

    async def delete(self) -> web.Response:
        app_id, key = self.match("app_id"), self.match("key")
        svc = self.svc(app_service_key)
        await svc.store_delete(app_id, self.user.user_id, key)
        return web.json_response({"status": "ok"})


class AppPeersView(BaseView):
    """``GET /api/apps/{app_id}/peers`` — list confirmed federation peers."""

    async def get(self) -> web.Response:
        svc = self.svc(app_federation_service_key)
        peers = await svc.list_peers()
        return self._json({"peers": peers})


class AppSessionsView(BaseView):
    """``POST /api/apps/{app_id}/sessions`` — open a cross-household app session."""

    async def post(self) -> web.Response:
        app_id = self.match("app_id")
        body = await self.body()
        peer_instance_id = body.get("peer_instance_id")
        if not isinstance(peer_instance_id, str) or not peer_instance_id:
            return error_response(
                400, "UNPROCESSABLE", "peer_instance_id must be a non-empty string."
            )
        svc = self.svc(app_federation_service_key)
        session_id = await svc.open_session(
            app_id=app_id,
            peer_instance_id=peer_instance_id,
            actor_user_id=self.user.user_id,
        )
        return self._json({"session_id": session_id}, status=201)


class AppMessagesView(BaseView):
    """``POST /api/apps/{app_id}/messages`` — send an app-layer message to a peer."""

    async def post(self) -> web.Response:
        app_id = self.match("app_id")
        body = await self.body()
        session_id = body.get("session_id")
        peer_instance_id = body.get("peer_instance_id")
        payload = body.get("payload")

        if not isinstance(session_id, str) or not session_id:
            return error_response(
                400, "UNPROCESSABLE", "session_id must be a non-empty string."
            )
        if not isinstance(peer_instance_id, str) or not peer_instance_id:
            return error_response(
                400, "UNPROCESSABLE", "peer_instance_id must be a non-empty string."
            )
        if payload is None:
            return error_response(400, "UNPROCESSABLE", "payload is required.")

        # Guard against excessively large payloads before forwarding to
        # the federation layer.  JSON-encode to get a byte-accurate count.
        try:
            encoded_size = len(json.dumps(payload).encode())
        except TypeError, ValueError:
            return error_response(
                400, "UNPROCESSABLE", "payload must be JSON-serialisable."
            )
        if encoded_size > _MAX_APP_PAYLOAD_BYTES:
            return error_response(
                413,
                "PAYLOAD_TOO_LARGE",
                f"payload exceeds the {_MAX_APP_PAYLOAD_BYTES // 1024} KiB limit.",
            )

        svc = self.svc(app_federation_service_key)
        await svc.send_message(
            app_id=app_id,
            session_id=session_id,
            peer_instance_id=peer_instance_id,
            payload=payload,
            actor_user_id=self.user.user_id,
        )
        return web.json_response({"ok": True})
