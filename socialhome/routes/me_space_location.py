"""Route handler — ``GET /api/me/space-location-sharing``.

Returns the list of spaces the authenticated user belongs to where
``feature_location`` is enabled, together with the user's current
per-space opt-in flag.  This powers the Personal Settings → Privacy →
'Space location sharing' panel so users can see and audit their
per-space sharing in one place.

The mutation path (enabling / disabling sharing per space) is handled
by the existing ``PATCH /api/spaces/{id}/members/me/location-sharing``
endpoint in ``routes/spaces.py`` — no new mutation endpoint is needed
here.
"""

from __future__ import annotations

from aiohttp import web

from ..app_keys import space_repo_key
from .base import BaseView


class MeSpaceLocationSharingView(BaseView):
    """``GET /api/me/space-location-sharing``."""

    async def get(self) -> web.Response:
        user = self.user
        space_repo = self.request.app[space_repo_key]
        rows = await space_repo.list_user_memberships_with_location_feature(
            user.user_id
        )
        return self._json({"spaces": rows})
