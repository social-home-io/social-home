"""Public-publish routes for highlights — ``/api/highlights/{id}/publish*``.

Author-only HTTP surface that drives :class:`HighlightPublicationService`:

* ``POST   /api/highlights/{id}/publish``        body ``{gfs_id, label?}`` → ``{url, token, label}``
* ``GET    /api/highlights/{id}/publish``        local read of the cached flag
* ``DELETE /api/highlights/{id}/publish``        full unpublish (drops every token)
* ``DELETE /api/highlights/{id}/publish/{token}`` revoke a single share token
* ``POST   /api/highlights/{id}/publish/og``     upload an OG-card thumbnail

Errors flow through :class:`BaseView._iter` — the service raises
:class:`HighlightNotFoundError` (→ 404) on author mismatches and
:class:`HighlightPublicationError` (→ 502) on GFS round-trip failures.
"""

from __future__ import annotations

import base64
import binascii

from aiohttp import web

from ..app_keys import (
    highlight_publication_service_key,
    highlight_repo_key,
)
from ..security import error_response
from .base import BaseView


class HighlightPublishView(BaseView):
    """``POST|GET|DELETE /api/highlights/{id}/publish``."""

    async def post(self) -> web.Response:
        ctx = self.user
        if ctx is None:
            return error_response(401, "UNAUTHENTICATED", "Login required.")
        body = await self.body()
        gfs_id = str(body.get("gfs_id") or "").strip()
        if not gfs_id:
            return error_response(422, "UNPROCESSABLE", "gfs_id is required.")
        label_raw = body.get("label")
        label = str(label_raw).strip() if label_raw else None
        svc = self.svc(highlight_publication_service_key)
        result = await svc.publish(
            self.match("id"),
            ctx.user_id,
            gfs_id=gfs_id,
            label=label,
        )
        return self._json(result, status=201)

    async def get(self) -> web.Response:
        """Read-only snapshot of the local publication flag.

        We don't proxy to GFS for the token list yet (PR2 frontend
        toggle ships that view); the SPA only needs to know whether
        *some* publication is in flight.
        """
        ctx = self.user
        if ctx is None:
            return error_response(401, "UNAUTHENTICATED", "Login required.")
        repo = self.svc(highlight_repo_key)
        highlight = await repo.get_highlight(self.match("id"))
        if highlight is None or highlight.author_user_id != ctx.user_id:
            return error_response(404, "NOT_FOUND", "Highlight not found.")
        return self._json(
            {
                "published": highlight.public_gfs_id is not None,
                "gfs_id": highlight.public_gfs_id,
                "published_at": highlight.public_published_at,
            }
        )

    async def delete(self) -> web.Response:
        ctx = self.user
        if ctx is None:
            return error_response(401, "UNAUTHENTICATED", "Login required.")
        svc = self.svc(highlight_publication_service_key)
        await svc.unpublish(self.match("id"), ctx.user_id)
        return self._json({"unpublished": True})


class HighlightPublishTokenView(BaseView):
    """``DELETE /api/highlights/{id}/publish/{token}``."""

    async def delete(self) -> web.Response:
        ctx = self.user
        if ctx is None:
            return error_response(401, "UNAUTHENTICATED", "Login required.")
        svc = self.svc(highlight_publication_service_key)
        await svc.revoke_token(
            self.match("id"),
            ctx.user_id,
            token=self.match("token"),
        )
        return self._json({"revoked": True})


class HighlightPublishOgView(BaseView):
    """``POST /api/highlights/{id}/publish/og`` — author uploads a cached
    thumbnail for the public OG card.

    Body: ``{image_b64: str}``. The SPA reads the first frame's blob
    and base64-encodes it. Forwarded to the publishing GFS via
    :meth:`HighlightPublicationService.upload_og_thumbnail`. Returns the
    canonical OG URL the SPA can show alongside the share link.
    """

    async def post(self) -> web.Response:
        ctx = self.user
        if ctx is None:
            return error_response(401, "UNAUTHENTICATED", "Login required.")
        body = await self.body()
        b64 = str(body.get("image_b64") or "")
        if not b64:
            return error_response(422, "UNPROCESSABLE", "image_b64 is required.")
        try:
            jpeg = base64.b64decode(b64, validate=True)
        except ValueError, binascii.Error:
            return error_response(
                422, "UNPROCESSABLE", "image_b64 is not valid base64."
            )
        svc = self.svc(highlight_publication_service_key)
        url = await svc.upload_og_thumbnail(
            self.match("id"),
            ctx.user_id,
            jpeg_bytes=jpeg,
        )
        return self._json({"url": url})
