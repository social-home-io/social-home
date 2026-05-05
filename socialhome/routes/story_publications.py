"""Public-publish routes for stories — ``/api/stories/{id}/publish*``.

Author-only HTTP surface that drives :class:`StoryPublicationService`:

* ``POST   /api/stories/{id}/publish``        body ``{gfs_id, label?}`` → ``{url, token, label}``
* ``GET    /api/stories/{id}/publish``        local read of the cached flag
* ``DELETE /api/stories/{id}/publish``        full unpublish (drops every token)
* ``DELETE /api/stories/{id}/publish/{token}`` revoke a single share token

Errors flow through :class:`BaseView._iter` — the service raises
:class:`StoryNotFoundError` (→ 404) on author mismatches and
:class:`StoryPublicationError` (→ 502) on GFS round-trip failures.
"""

from __future__ import annotations

from aiohttp import web

from ..app_keys import (
    story_publication_service_key,
    story_repo_key,
)
from ..security import error_response
from .base import BaseView


class StoryPublishView(BaseView):
    """``POST|GET|DELETE /api/stories/{id}/publish``."""

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
        svc = self.svc(story_publication_service_key)
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
        repo = self.svc(story_repo_key)
        story = await repo.get_story(self.match("id"))
        if story is None or story.author_user_id != ctx.user_id:
            return error_response(404, "NOT_FOUND", "Story not found.")
        return self._json(
            {
                "published": story.public_gfs_id is not None,
                "gfs_id": story.public_gfs_id,
                "published_at": story.public_published_at,
            }
        )

    async def delete(self) -> web.Response:
        ctx = self.user
        if ctx is None:
            return error_response(401, "UNAUTHENTICATED", "Login required.")
        svc = self.svc(story_publication_service_key)
        await svc.unpublish(self.match("id"), ctx.user_id)
        return self._json({"unpublished": True})


class StoryPublishTokenView(BaseView):
    """``DELETE /api/stories/{id}/publish/{token}``."""

    async def delete(self) -> web.Response:
        ctx = self.user
        if ctx is None:
            return error_response(401, "UNAUTHENTICATED", "Login required.")
        svc = self.svc(story_publication_service_key)
        await svc.revoke_token(
            self.match("id"),
            ctx.user_id,
            token=self.match("token"),
        )
        return self._json({"revoked": True})
