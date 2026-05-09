"""GFS public-highlight routes (§highlights_public).

Two surfaces:

* **Signed wire endpoints** under ``/gfs/highlights/*`` and
  ``/gfs/highlight_tokens/*`` — used by author SH instances to publish,
  mint additional tokens, revoke single tokens, and unpublish. All
  bodies are Ed25519-signed and reuse the existing
  :func:`_rtc_authenticate` middleware in :mod:`global_server.routes.rtc`.
* **Public landing page** ``GET /highlight/{instance_id}/{highlight_id}/{token}`` —
  served as plain HTML to anyone with the URL. PR1 returns a placeholder
  with ``200 / 410 / 503`` shape; PR2 wires the WebRTC bootstrap script.

The author's instance is the only entity that holds highlight bytes; this
GFS only relays SDP/ICE later.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging

from aiohttp import web

from .. import app_keys as K
from .base import GfsBaseView
from .rtc import _rtc_authenticate

log = logging.getLogger(__name__)


# ─── Signed wire endpoints (author SH → GFS) ─────────────────────────────


class HighlightPublishView(GfsBaseView):
    """``POST /gfs/highlights/{highlight_id}/publish``.

    Records a fresh publication and mints the first share token.
    """

    async def post(self) -> web.Response:
        result = await _rtc_authenticate(self)
        if isinstance(result, web.Response):
            return result
        body, instance_id = result
        highlight_id = self.match("highlight_id")
        if str(body.get("highlight_id") or "") != highlight_id:
            return web.json_response(
                {"error": "highlight_id_mismatch"},
                status=422,
            )
        try:
            expires_at = int(body.get("expires_at") or 0)
        except TypeError, ValueError:
            return web.json_response({"error": "invalid_expires_at"}, status=422)
        if expires_at <= 0:
            return web.json_response({"error": "invalid_expires_at"}, status=422)
        label_raw = body.get("label")
        label = str(label_raw) if label_raw else None

        registry = self.svc(K.gfs_highlight_pub_service_key)
        # ``_rtc_authenticate`` already verified the Ed25519 signature
        # over the canonical body. PR2 will additionally cache the raw
        # signature for offline audit; for PR1 we record an empty
        # string and rely on the live-verification path to gate writes.
        token, url = await registry.record_publish(
            highlight_id=highlight_id,
            instance_id=instance_id,
            expires_at=expires_at,
            publish_signature="",
            label=label,
        )
        return web.json_response(
            {"token": token.token, "url": url, "label": token.label},
            status=201,
        )


class HighlightTokenMintView(GfsBaseView):
    """``POST /gfs/highlights/{highlight_id}/tokens`` — mint another share token
    under an existing publication."""

    async def post(self) -> web.Response:
        result = await _rtc_authenticate(self)
        if isinstance(result, web.Response):
            return result
        body, instance_id = result
        highlight_id = self.match("highlight_id")
        label_raw = body.get("label")
        label = str(label_raw) if label_raw else None

        registry = self.svc(K.gfs_highlight_pub_service_key)
        try:
            token, url = await registry.mint_token(
                highlight_id=highlight_id,
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


class HighlightTokenRevokeView(GfsBaseView):
    """``POST /gfs/highlight_tokens/{token}/revoke``."""

    async def post(self) -> web.Response:
        result = await _rtc_authenticate(self)
        if isinstance(result, web.Response):
            return result
        _body, instance_id = result
        token = self.match("token")
        registry = self.svc(K.gfs_highlight_pub_service_key)
        revoked = await registry.revoke_token(token, instance_id)
        if not revoked:
            return web.json_response({"error": "not_found"}, status=404)
        return web.json_response({"status": "ok"})


class HighlightUnpublishView(GfsBaseView):
    """``POST /gfs/highlights/{highlight_id}/unpublish`` — drop the publication
    row + every token under it (CASCADE)."""

    async def post(self) -> web.Response:
        result = await _rtc_authenticate(self)
        if isinstance(result, web.Response):
            return result
        _body, instance_id = result
        highlight_id = self.match("highlight_id")
        registry = self.svc(K.gfs_highlight_pub_service_key)
        removed = await registry.remove_publish(highlight_id, instance_id)
        if not removed:
            return web.json_response({"error": "not_found"}, status=404)
        return web.json_response({"status": "ok"})


class HighlightOgUploadView(GfsBaseView):
    """``POST /gfs/highlights/{highlight_id}/og`` — author SH uploads the cached
    OG-card thumbnail for an existing publication.

    Body is the same Ed25519-signed JSON envelope the rest of
    ``/gfs/highlights/*`` uses; the JPEG is base64-encoded into the
    ``image_b64`` field so we can re-use the canonical-body signing
    helper without juggling multipart boundaries.
    """

    async def post(self) -> web.Response:
        result = await _rtc_authenticate(self)
        if isinstance(result, web.Response):
            return result
        body, instance_id = result
        highlight_id = self.match("highlight_id")
        b64 = str(body.get("image_b64") or "")
        if not b64:
            return web.json_response({"error": "missing_image"}, status=422)
        try:
            raw = base64.b64decode(b64, validate=True)
        except ValueError, binascii.Error:
            return web.json_response({"error": "invalid_b64"}, status=422)
        registry = self.svc(K.gfs_highlight_pub_service_key)
        # Publication must already exist — author publishes first, then
        # uploads the preview. Avoids leaking storage to anyone with
        # an instance key.
        pub = await registry.get_publication(highlight_id, instance_id)
        if pub is None:
            return web.json_response({"error": "not_published"}, status=404)
        try:
            filename = await registry.store_og_thumbnail(
                highlight_id=highlight_id,
                instance_id=instance_id,
                jpeg_bytes=raw,
            )
        except ValueError as exc:
            return web.json_response({"error": str(exc)}, status=422)
        return web.json_response(
            {
                "status": "ok",
                "url": registry.og_image_url(instance_id, highlight_id),
                "filename": filename,
            }
        )


class HighlightOgImageView(GfsBaseView):
    """``GET /highlight/{instance_id}/{highlight_id}/og.jpg`` — public, no token.

    Anonymous social-card crawlers (Twitter, Slack, iMessage, etc.)
    can fetch this directly without the share token. Returns 404 when
    the author hasn't uploaded a thumbnail or the publication is gone.
    """

    async def get(self) -> web.StreamResponse:
        instance_id = self.match("instance_id")
        highlight_id = self.match("highlight_id")
        registry = self.svc(K.gfs_highlight_pub_service_key)
        pub = await registry.get_publication(highlight_id, instance_id)
        if pub is None or pub.og_thumbnail_filename is None:
            return web.Response(status=404, text="not_found")
        try:
            path = registry.og_thumbnail_path(pub.og_thumbnail_filename)
        except ValueError:  # pragma: no cover - DB stored garbage
            return web.Response(status=404, text="not_found")
        if not path.is_file():
            return web.Response(status=404, text="not_found")
        return web.FileResponse(
            path,
            headers={"Cache-Control": "public, max-age=300"},
        )


# ─── Public landing page ─────────────────────────────────────────────────


class HighlightPublicLandingView(GfsBaseView):
    """``GET /highlight/{instance_id}/{highlight_id}/{token}`` — public viewer.

    PR1 returns a static placeholder body and the 410 / 503 logic so
    the publish/revoke contract can be tested end-to-end without the
    PR2 WebRTC layer. PR2 swaps the body for the actual bootstrap
    HTML + ``<script src="/static/highlight-public-viewer.js">``.
    """

    async def get(self) -> web.Response:
        instance_id = self.match("instance_id")
        highlight_id = self.match("highlight_id")
        token = self.match("token")

        registry = self.svc(K.gfs_highlight_pub_service_key)
        resolved = await registry.resolve_token(token)
        if resolved is None:
            return _gone_html(instance_id, highlight_id)

        # Token-vs-URL consistency check — guards against someone
        # crafting a URL that mixes the right token with a wrong
        # highlight_id (e.g. trying to enumerate other highlights on the
        # same instance).
        pub = resolved.publication
        if pub.highlight_id != highlight_id or pub.instance_id != instance_id:
            return _gone_html(instance_id, highlight_id)

        if not await registry.author_online(instance_id):
            return _unavailable_html(instance_id, highlight_id)

        # Boot payload — the bootstrap JS reads this <script id="boot">
        # tag for the (instance_id, highlight_id, token) triple it needs to
        # POST /gfs/highlight_rtc/offer. Keeping the values inline lets the
        # JS bundle stay zero-state — no URL parsing on the client side.
        boot = json.dumps(
            {"instanceId": instance_id, "highlightId": highlight_id, "token": token}
        )
        # Author may have opted into a cached social-preview thumbnail
        # (§highlights_public OG card). Only emit ``og:image`` when we
        # know the file is on disk — empty meta tags break some
        # crawlers' fallback logic.
        og_image_tag = ""
        if pub.og_thumbnail_filename:
            og_url = registry.og_image_url(instance_id, highlight_id)
            og_image_tag = (
                f"<meta property='og:image' content='{og_url}'>"
                f"<meta name='twitter:card' content='summary_large_image'>"
                f"<meta name='twitter:image' content='{og_url}'>"
            )
        body = (
            "<!doctype html><html lang='en'><head>"
            "<meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            "<title>Highlight</title>"
            "<meta property='og:title' content='A highlight shared with you'>"
            "<meta property='og:type' content='website'>"
            f"{og_image_tag}"
            "<style>"
            "html,body{margin:0;padding:0;background:#111;color:#eee;"
            "font-family:system-ui,sans-serif;height:100%;}"
            "#root{height:100vh;display:flex;}"
            ".highlight-viewer{display:flex;flex-direction:column;width:100%;}"
            ".progress{display:flex;gap:4px;padding:12px;}"
            ".progress .seg{flex:1;height:3px;background:rgba(255,255,255,.2);border-radius:2px;}"
            ".progress .seg.done{background:rgba(255,255,255,.6);}"
            ".progress .seg.active{background:#fff;}"
            ".stage{flex:1;display:flex;align-items:center;justify-content:center;"
            "padding:0 12px;position:relative;}"
            ".stage img,.stage video{max-width:100%;max-height:100%;border-radius:8px;}"
            ".caption{position:absolute;bottom:24px;padding:8px 14px;"
            "background:rgba(0,0,0,.5);border-radius:8px;max-width:80%;}"
            ".highlight-error,.highlight-end{padding:24px;text-align:center;width:100%;}"
            ".status{color:#aaa;font-size:.9em;text-align:center;}"
            "</style>"
            "</head><body>"
            "<div id='root'></div>"
            f"<script id='boot' type='application/json'>{boot}</script>"
            "<script type='module' src='/static/highlight_public_viewer.js'></script>"
            "</body></html>"
        )
        return web.Response(text=body, content_type="text/html", status=200)


# ─── Internal helpers ────────────────────────────────────────────────────


_FALLBACK_STYLE = (
    "<style>"
    "html,body{margin:0;padding:0;background:#faf6f1;color:#1f1916;"
    "font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;"
    "min-height:100%;}"
    "main{max-width:560px;margin:0 auto;padding:48px 24px;"
    "text-align:center;}"
    "h1{font-family:'Lora','Iowan Old Style','Palatino Linotype',serif;"
    "font-size:28px;letter-spacing:-0.01em;margin:0 0 12px;}"
    "p{color:#5b4f48;line-height:1.55;margin:0 0 16px;}"
    "small{color:#9a8d83;font-size:11px;}"
    ".accent-bar{height:6px;background:#ce5d3e;border-radius:3px;"
    "max-width:120px;margin:0 auto 24px;}"
    "</style>"
)


def _gone_html(instance_id: str, highlight_id: str) -> web.Response:
    body = (
        "<!doctype html><html lang='en'><head>"
        "<meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>Highlight expired</title>"
        f"{_FALLBACK_STYLE}"
        "</head><body><main>"
        "<div class='accent-bar'></div>"
        "<h1>This highlight has ended</h1>"
        "<p>The link is no longer valid — the author may have "
        "unpublished it, the highlight expired, or this token was revoked.</p>"
        f"<p><small>{instance_id}/{highlight_id}</small></p>"
        "</main></body></html>"
    )
    return web.Response(text=body, content_type="text/html", status=410)


def _unavailable_html(instance_id: str, highlight_id: str) -> web.Response:
    body = (
        "<!doctype html><html lang='en'><head>"
        "<meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>Highlight unavailable</title>"
        "<meta http-equiv='refresh' content='10'>"
        f"{_FALLBACK_STYLE}"
        "</head><body><main>"
        "<div class='accent-bar'></div>"
        "<h1>Currently unavailable</h1>"
        "<p>The author's instance is offline. This page will retry "
        "automatically in 10 seconds.</p>"
        f"<p><small>{instance_id}/{highlight_id}</small></p>"
        "</main></body></html>"
    )
    return web.Response(text=body, content_type="text/html", status=503)
