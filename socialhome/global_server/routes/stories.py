"""GFS public-story routes (§stories_public).

Two surfaces:

* **Signed wire endpoints** under ``/gfs/stories/*`` and
  ``/gfs/story_tokens/*`` — used by author SH instances to publish,
  mint additional tokens, revoke single tokens, and unpublish. All
  bodies are Ed25519-signed and reuse the existing
  :func:`_rtc_authenticate` middleware in :mod:`global_server.routes.rtc`.
* **Public landing page** ``GET /story/{instance_id}/{story_id}/{token}`` —
  served as plain HTML to anyone with the URL. PR1 returns a placeholder
  with ``200 / 410 / 503`` shape; PR2 wires the WebRTC bootstrap script.

The author's instance is the only entity that holds story bytes; this
GFS only relays SDP/ICE later.
"""

from __future__ import annotations

import logging

from aiohttp import web

from .. import app_keys as K
from .base import GfsBaseView
from .rtc import _rtc_authenticate

log = logging.getLogger(__name__)


# ─── Signed wire endpoints (author SH → GFS) ─────────────────────────────


class StoryPublishView(GfsBaseView):
    """``POST /gfs/stories/{story_id}/publish``.

    Records a fresh publication and mints the first share token.
    """

    async def post(self) -> web.Response:
        result = await _rtc_authenticate(self)
        if isinstance(result, web.Response):
            return result
        body, instance_id = result
        story_id = self.match("story_id")
        if str(body.get("story_id") or "") != story_id:
            return web.json_response(
                {"error": "story_id_mismatch"},
                status=422,
            )
        try:
            expires_at = int(body.get("expires_at") or 0)
        except (TypeError, ValueError):
            return web.json_response({"error": "invalid_expires_at"}, status=422)
        if expires_at <= 0:
            return web.json_response({"error": "invalid_expires_at"}, status=422)
        label_raw = body.get("label")
        label = str(label_raw) if label_raw else None

        registry = self.svc(K.gfs_story_pub_service_key)
        # ``_rtc_authenticate`` already verified the Ed25519 signature
        # over the canonical body. PR2 will additionally cache the raw
        # signature for offline audit; for PR1 we record an empty
        # string and rely on the live-verification path to gate writes.
        token, url = await registry.record_publish(
            story_id=story_id,
            instance_id=instance_id,
            expires_at=expires_at,
            publish_signature="",
            label=label,
        )
        return web.json_response(
            {"token": token.token, "url": url, "label": token.label},
            status=201,
        )


class StoryTokenMintView(GfsBaseView):
    """``POST /gfs/stories/{story_id}/tokens`` — mint another share token
    under an existing publication."""

    async def post(self) -> web.Response:
        result = await _rtc_authenticate(self)
        if isinstance(result, web.Response):
            return result
        body, instance_id = result
        story_id = self.match("story_id")
        label_raw = body.get("label")
        label = str(label_raw) if label_raw else None

        registry = self.svc(K.gfs_story_pub_service_key)
        try:
            token, url = await registry.mint_token(
                story_id=story_id,
                instance_id=instance_id,
                label=label,
            )
        except LookupError:
            return web.json_response(
                {"error": "publication_not_found"},
                status=404,
            )
        return web.json_response(
            {"token": token.token, "url": url, "label": token.label},
            status=201,
        )


class StoryTokenRevokeView(GfsBaseView):
    """``POST /gfs/story_tokens/{token}/revoke``."""

    async def post(self) -> web.Response:
        result = await _rtc_authenticate(self)
        if isinstance(result, web.Response):
            return result
        _body, instance_id = result
        token = self.match("token")
        registry = self.svc(K.gfs_story_pub_service_key)
        revoked = await registry.revoke_token(token, instance_id)
        if not revoked:
            return web.json_response({"error": "not_found"}, status=404)
        return web.json_response({"status": "ok"})


class StoryUnpublishView(GfsBaseView):
    """``POST /gfs/stories/{story_id}/unpublish`` — drop the publication
    row + every token under it (CASCADE)."""

    async def post(self) -> web.Response:
        result = await _rtc_authenticate(self)
        if isinstance(result, web.Response):
            return result
        _body, instance_id = result
        story_id = self.match("story_id")
        registry = self.svc(K.gfs_story_pub_service_key)
        removed = await registry.remove_publish(story_id, instance_id)
        if not removed:
            return web.json_response({"error": "not_found"}, status=404)
        return web.json_response({"status": "ok"})


# ─── Public landing page ─────────────────────────────────────────────────


class StoryPublicLandingView(GfsBaseView):
    """``GET /story/{instance_id}/{story_id}/{token}`` — public viewer.

    PR1 returns a static placeholder body and the 410 / 503 logic so
    the publish/revoke contract can be tested end-to-end without the
    PR2 WebRTC layer. PR2 swaps the body for the actual bootstrap
    HTML + ``<script src="/static/story-public-viewer.js">``.
    """

    async def get(self) -> web.Response:
        instance_id = self.match("instance_id")
        story_id = self.match("story_id")
        token = self.match("token")

        registry = self.svc(K.gfs_story_pub_service_key)
        resolved = await registry.resolve_token(token)
        if resolved is None:
            return _gone_html(instance_id, story_id)

        # Token-vs-URL consistency check — guards against someone
        # crafting a URL that mixes the right token with a wrong
        # story_id (e.g. trying to enumerate other stories on the
        # same instance).
        pub = resolved.publication
        if pub.story_id != story_id or pub.instance_id != instance_id:
            return _gone_html(instance_id, story_id)

        if not await registry.author_online(instance_id):
            return _unavailable_html(instance_id, story_id)

        # PR1 placeholder — the actual viewer JS lands in PR2.
        body = (
            "<!doctype html><html><head>"
            "<meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            "<title>Story</title>"
            "</head><body>"
            "<h1>Story coming soon</h1>"
            "<p>The owner has shared a story with you. The full viewer "
            "is being rolled out — refresh in a moment.</p>"
            f"<p><small>{instance_id}/{story_id}</small></p>"
            "</body></html>"
        )
        return web.Response(text=body, content_type="text/html", status=200)


# ─── Internal helpers ────────────────────────────────────────────────────


def _gone_html(instance_id: str, story_id: str) -> web.Response:
    body = (
        "<!doctype html><html><head>"
        "<meta charset='utf-8'>"
        "<title>Story expired</title>"
        "</head><body>"
        "<h1>This story has ended</h1>"
        "<p>The link is no longer valid — the author may have "
        "unpublished it, the story expired, or this token was revoked.</p>"
        f"<p><small>{instance_id}/{story_id}</small></p>"
        "</body></html>"
    )
    return web.Response(text=body, content_type="text/html", status=410)


def _unavailable_html(instance_id: str, story_id: str) -> web.Response:
    body = (
        "<!doctype html><html><head>"
        "<meta charset='utf-8'>"
        "<title>Story unavailable</title>"
        "<meta http-equiv='refresh' content='10'>"
        "</head><body>"
        "<h1>Currently unavailable</h1>"
        "<p>The author's instance is offline. This page will retry "
        "automatically in 10 seconds.</p>"
        f"<p><small>{instance_id}/{story_id}</small></p>"
        "</body></html>"
    )
    return web.Response(text=body, content_type="text/html", status=503)
