"""GFS connection routes — /api/gfs/*.

Manages Global Federation Server pairing, disconnection, and
per-space publication control.
"""

from __future__ import annotations

from dataclasses import asdict

from aiohttp import web

from .. import app_keys as K
from ..security import error_response
from ..services.gfs_connection_service import GfsConnectionError
from .base import BaseView


_DEFAULT_HEALTH: dict = {"connected": False, "last_error": None}


def _conn_dict(conn, health: dict | None = None) -> dict:
    """Public-shape view of a :class:`GfsConnection`.

    ``status`` stays the stored PAIRING state (active/pending/suspended).
    ``health`` carries the supervisor's LIVE WS signal — ``connected``
    (is a socket up right now) + ``last_error`` (the last auth/close
    reason when not connected) — so the SPA reflects real liveness rather
    than treating a stored ``status='active'`` as "connected".
    """
    d = asdict(conn)
    # Remove sensitive key material from the API response.
    d.pop("public_key", None)
    h = health if health is not None else _DEFAULT_HEALTH
    d["connected"] = bool(h.get("connected", False))
    d["last_error"] = h.get("last_error")
    return d


def _pub_dict(pub) -> dict:
    """JSON shape of a :class:`GfsSpacePublication` for the SPA."""
    return {
        "space_id": pub.space_id,
        "gfs_connection_id": pub.gfs_connection_id,
        "published_at": pub.published_at,
        "status": pub.status,
    }


class GfsConnectionCollectionView(BaseView):
    """``GET /api/gfs/connections`` — list.
    ``POST /api/gfs/connections`` — pair via QR payload.
    """

    async def get(self) -> web.Response:
        ctx = self.user
        if ctx is None or ctx.user_id is None:
            return error_response(401, "UNAUTHENTICATED", "Authentication required.")
        svc = self.svc(K.gfs_connection_service_key)
        connections = await svc.list_connections()
        supervisor = self.request.app.get(K.gfs_ws_supervisor_key)
        return web.json_response(
            [
                _conn_dict(
                    c,
                    supervisor.connection_health(c.id)
                    if supervisor is not None
                    else None,
                )
                for c in connections
            ]
        )

    async def post(self) -> web.Response:
        ctx = self.user
        if ctx is None or ctx.user_id is None:
            return error_response(401, "UNAUTHENTICATED", "Authentication required.")
        if not ctx.is_admin:
            return error_response(403, "FORBIDDEN", "Admin only.")
        body = await self.body()
        adapter = self.svc(K.platform_adapter_key)
        own_base = await adapter.get_federation_base()
        if not own_base:
            return error_response(
                422,
                "NOT_CONFIGURED",
                "External URL is not configured — set it before pairing a GFS.",
            )
        svc = self.svc(K.gfs_connection_service_key)
        own_instance_id = self.request.app[K.instance_id_key]
        own_pk: bytes = self.request.app[K.instance_public_key_key]
        own_keywrap_pk: bytes = self.request.app[K.instance_keywrap_public_key_key]
        own_keywrap_sig: str = self.request.app[K.instance_keywrap_sig_key]
        config = self.request.app[K.config_key]
        try:
            conn = await svc.pair(
                body,
                own_instance_id=own_instance_id,
                own_public_key_hex=own_pk.hex(),
                own_inbox_url=own_base,
                own_display_name=config.instance_name,
                own_keywrap_public_key_hex=own_keywrap_pk.hex(),
                own_keywrap_sig=own_keywrap_sig,
            )
        except GfsConnectionError as exc:
            return error_response(422, "GFS_PAIRING_FAILED", str(exc))
        return web.json_response(_conn_dict(conn), status=201)


class GfsConnectionDetailView(BaseView):
    """``GET /api/gfs/connections/{id}`` — detail.
    ``DELETE /api/gfs/connections/{id}`` — disconnect.
    """

    async def get(self) -> web.Response:
        ctx = self.user
        if ctx is None or ctx.user_id is None:
            return error_response(401, "UNAUTHENTICATED", "Authentication required.")
        gfs_id = self.match("id")
        repo = self.svc(K.gfs_connection_repo_key)
        conn = await repo.get(gfs_id)
        if conn is None:
            return error_response(404, "NOT_FOUND", "GFS connection not found.")
        supervisor = self.request.app.get(K.gfs_ws_supervisor_key)
        health = (
            supervisor.connection_health(gfs_id) if supervisor is not None else None
        )
        return web.json_response(_conn_dict(conn, health))

    async def delete(self) -> web.Response:
        ctx = self.user
        if ctx is None or ctx.user_id is None:
            return error_response(401, "UNAUTHENTICATED", "Authentication required.")
        if not ctx.is_admin:
            return error_response(403, "FORBIDDEN", "Admin only.")
        gfs_id = self.match("id")
        svc = self.svc(K.gfs_connection_service_key)
        try:
            await svc.disconnect(gfs_id)
        except GfsConnectionError as exc:
            return error_response(404, "NOT_FOUND", str(exc))
        return web.Response(status=204)


class GfsSpacePublishView(BaseView):
    """``POST /api/spaces/{id}/publish/{gfs_id}`` — publish.
    ``DELETE /api/spaces/{id}/publish/{gfs_id}`` — unpublish.
    """

    async def post(self) -> web.Response:
        ctx = self.user
        if ctx is None or ctx.user_id is None:
            return error_response(401, "UNAUTHENTICATED", "Authentication required.")
        if not ctx.is_admin:
            return error_response(403, "FORBIDDEN", "Admin only.")
        space_id = self.match("id")
        gfs_id = self.match("gfs_id")
        svc = self.svc(K.gfs_connection_service_key)
        try:
            pub = await svc.publish_space(space_id, gfs_id)
        except GfsConnectionError as exc:
            return error_response(422, "GFS_PUBLISH_FAILED", str(exc))
        return web.json_response(_pub_dict(pub))

    async def delete(self) -> web.Response:
        ctx = self.user
        if ctx is None or ctx.user_id is None:
            return error_response(401, "UNAUTHENTICATED", "Authentication required.")
        if not ctx.is_admin:
            return error_response(403, "FORBIDDEN", "Admin only.")
        space_id = self.match("id")
        gfs_id = self.match("gfs_id")
        svc = self.svc(K.gfs_connection_service_key)
        try:
            await svc.unpublish_space(space_id, gfs_id)
        except GfsConnectionError as exc:
            return error_response(422, "GFS_UNPUBLISH_FAILED", str(exc))
        return web.Response(status=204)


class GfsSpacePublicationsView(BaseView):
    """``GET /api/spaces/{id}/publications`` — list this space's GFS
    publications (with per-row ``status``).

    Drives the space's federation panel in the SPA, fetched on mount.
    Admin-only, mirroring the gating on
    :class:`GfsSpacePublishView.post`.
    """

    async def get(self) -> web.Response:
        ctx = self.user
        if ctx is None or ctx.user_id is None:
            return error_response(401, "UNAUTHENTICATED", "Authentication required.")
        if not ctx.is_admin:
            return error_response(403, "FORBIDDEN", "Admin only.")
        space_id = self.match("id")
        repo = self.svc(K.gfs_connection_repo_key)
        pubs = await repo.list_publications_for_space(space_id)
        return web.json_response([_pub_dict(p) for p in pubs])


class GfsPublicationsView(BaseView):
    """``GET /api/gfs/publications`` — §A5 admin list every
    (space, GFS) publication currently active across all pairings.
    Used by the admin Spaces tab to render a "currently published
    to" table with per-row Unpublish button.
    """

    async def get(self) -> web.Response:
        ctx = self.user
        if ctx is None or ctx.user_id is None:
            return error_response(401, "UNAUTHENTICATED", "Login required.")
        if not ctx.is_admin:
            return error_response(403, "FORBIDDEN", "Admin only.")
        repo = self.svc(K.gfs_connection_repo_key)
        rows = await repo.list_publications_all()
        return web.json_response({"publications": rows})


class GfsAppealView(BaseView):
    """``POST /api/gfs/connections/{gfs_id}/appeal`` — file an appeal.

    Body: ``{target_type: 'space'|'instance', target_id, message}``.
    Sends ``POST /gfs/appeal`` (Ed25519-signed) to the given GFS; on
    success the admin portal's Appeals tab will surface the new row.
    """

    async def post(self) -> web.Response:
        ctx = self.user
        if ctx is None or ctx.user_id is None:
            return error_response(
                401,
                "UNAUTHENTICATED",
                "Authentication required.",
            )
        if not ctx.is_admin:
            return error_response(403, "FORBIDDEN", "Admin only.")
        gfs_id = self.match("gfs_id")
        body = await self.body()
        target_type = str(body.get("target_type") or "")
        target_id = str(body.get("target_id") or "")
        message = str(body.get("message") or "").strip()
        if target_type not in ("space", "instance") or not target_id:
            return error_response(
                422,
                "UNPROCESSABLE",
                "target_type must be 'space'|'instance' and target_id required",
            )
        svc = self.svc(K.gfs_connection_service_key)
        signing_key = self.request.app[K.instance_signing_key_key]
        own_instance = self.request.app[K.instance_id_key]
        ok = await svc.send_appeal(
            gfs_id,
            target_type=target_type,
            target_id=target_id,
            message=message,
            from_instance=own_instance,
            signing_key=signing_key,
        )
        if not ok:
            return error_response(502, "GFS_APPEAL_FAILED", "GFS did not accept")
        return web.json_response({"status": "submitted"}, status=201)
