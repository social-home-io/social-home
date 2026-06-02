"""Public-viewer WebRTC signalling for the public-highlight flow.

These endpoints are anonymous on purpose — a non-Social-Home browser
can hit them without any Ed25519 key. Authentication is the share
token: the offer body must carry an ``(instance_id, highlight_id, token)``
triple that resolves to a live publication via
:class:`HighlightPublicationRegistry`.

The offer is stored in the same :class:`GfsRtcSession` table used by
SH↔SH sync (so the SDP plumbing stays shared) and is *also* pushed to
the author's SH over the existing GFS↔SH WebSocket as a
``highlight_signal`` frame. The author's SH replies with an SDP answer
via the signed :class:`HighlightRtcAnswerView`; the browser polls
:class:`HighlightRtcSessionView` until the answer + ICE list arrive.
"""

from __future__ import annotations

import asyncio
import logging

from aiohttp import web

from .. import app_keys as K
from .base import GfsBaseView
from .rtc import _rtc_authenticate, authenticate_relay_stream

log = logging.getLogger(__name__)

#: How long the guest's relay GET waits for the author SH to start
#: streaming before giving up with 503. Matches the viewer's WebRTC
#: poll budget so the fallback doesn't hang far longer than the primary.
RELAY_AUTHOR_CONNECT_TIMEOUT_SECONDS: float = 30.0

#: Author body is read in chunks this size and piped straight to the
#: guest — independent of the framing chunk size (pure byte passthrough).
RELAY_READ_CHUNK_BYTES: int = 64 * 1024


# ─── Public viewer surface (anonymous) ───────────────────────────────────


class HighlightRtcOfferView(GfsBaseView):
    """``POST /gfs/highlight_rtc/offer`` — viewer initiates signalling.

    Body: ``{instance_id, highlight_id, token, sdp}``. No signature — the
    token + the URL-segment match against ``gfs_highlight_tokens`` is the
    auth path. We push a ``highlight_signal`` frame to the author's WS so
    its :class:`HighlightSignalingHandler` knows to answer.
    """

    async def post(self) -> web.Response:
        body = await self.body_or_400()
        instance_id = str(body.get("instance_id") or "")
        highlight_id = str(body.get("highlight_id") or "")
        token = str(body.get("token") or "")
        sdp = str(body.get("sdp") or "")
        if not (instance_id and highlight_id and token and sdp):
            return web.json_response(
                {"error": "missing_fields"},
                status=422,
            )
        registry = self.svc(K.gfs_highlight_pub_service_key)
        resolved = await registry.resolve_token(token)
        if (
            resolved is None
            or resolved.publication.instance_id != instance_id
            or resolved.publication.highlight_id != highlight_id
        ):
            return web.json_response({"error": "gone"}, status=410)
        if not await registry.author_online(instance_id):
            return web.json_response({"error": "unavailable"}, status=503)

        rtc = self.svc(K.gfs_rtc_key)
        session_id = await rtc.offer(instance_id, sdp)
        # Push the signalling offer to the author's WS so its
        # ``HighlightSignalingHandler`` can spin up an answerer peer.
        ws_registry = self.svc(K.gfs_ws_registry_key)
        await ws_registry.send(
            instance_id,
            {
                "type": "highlight_signal",
                "kind": "offer",
                "session_id": session_id,
                "highlight_id": highlight_id,
                "token": token,
                "sdp": sdp,
            },
        )
        return web.json_response({"session_id": session_id}, status=201)


class HighlightRtcSessionView(GfsBaseView):
    """``GET /gfs/highlight_rtc/session/{session_id}`` — viewer polls for answer."""

    async def get(self) -> web.Response:
        session_id = self.match("session_id")
        rtc = self.svc(K.gfs_rtc_key)
        session = rtc.get_session(session_id)
        if session is None:
            return web.json_response({"error": "not_found"}, status=404)
        return web.json_response(
            {
                "session_id": session.session_id,
                "answer_sdp": session.answer_sdp,
                "ice_candidates": session.ice_candidates,
            }
        )


class HighlightRtcViewerIceView(GfsBaseView):
    """``POST /gfs/highlight_rtc/ice/viewer`` — viewer pushes an ICE candidate."""

    async def post(self) -> web.Response:
        body = await self.body_or_400()
        session_id = str(body.get("session_id") or "")
        candidate = body.get("candidate") or {}
        if not session_id or not isinstance(candidate, dict):
            return web.json_response({"error": "invalid"}, status=422)
        rtc = self.svc(K.gfs_rtc_key)
        try:
            await rtc.ice_candidate(session_id, candidate)
        except KeyError:
            return web.json_response({"error": "not_found"}, status=404)
        # Forward to the author so the answerer peer learns the
        # remote ICE list as it streams in.
        session = rtc.get_session(session_id)
        if session is not None:
            ws_registry = self.svc(K.gfs_ws_registry_key)
            await ws_registry.send(
                session.initiator_id,
                {
                    "type": "highlight_signal",
                    "kind": "ice",
                    "session_id": session_id,
                    "candidate": candidate,
                },
            )
        return web.json_response({"status": "ok"})


# ─── GFS-relay fallback (anon guest GET ⇄ signed author stream) ───────────


class HighlightRelayStreamView(GfsBaseView):
    """``GET /gfs/highlight_rtc/relay/{instance_id}/{highlight_id}`` — anon.

    Fallback for when the guest can't open a direct WebRTC DataChannel.
    Auth is the same share ``token`` (query param) the offer flow uses.
    We register a transient relay channel, push a ``relay_offer`` to the
    author over the WS, wait for the author to start streaming, then pipe
    the author's framed bytes straight to this chunked response. The GFS
    stores nothing — it is a pure passthrough for already-public content.
    """

    async def get(self) -> web.StreamResponse:
        instance_id = self.match("instance_id")
        highlight_id = self.match("highlight_id")
        token = self.request.query.get("token", "")
        if not token:
            return web.json_response({"error": "missing_token"}, status=422)

        registry = self.svc(K.gfs_highlight_pub_service_key)
        resolved = await registry.resolve_token(token)
        if (
            resolved is None
            or resolved.publication.instance_id != instance_id
            or resolved.publication.highlight_id != highlight_id
        ):
            return web.json_response({"error": "gone"}, status=410)
        if not await registry.author_online(instance_id):
            return web.json_response({"error": "unavailable"}, status=503)

        bridge = self.svc(K.gfs_relay_bridge_key)
        relay_id = bridge.create(target_instance_id=instance_id, scope=highlight_id)
        channel = bridge.get(relay_id)
        assert channel is not None  # just created

        ws_registry = self.svc(K.gfs_ws_registry_key)
        await ws_registry.send(
            instance_id,
            {
                "type": "highlight_signal",
                "kind": "relay_offer",
                "relay_id": relay_id,
                "highlight_id": highlight_id,
                "token": token,
            },
        )

        # Hold the request until the author actually starts streaming, so a
        # stalled author yields a clean 503 rather than an empty 200 body.
        try:
            await asyncio.wait_for(
                channel.connected.wait(),
                timeout=RELAY_AUTHOR_CONNECT_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError, TimeoutError:
            bridge.close(relay_id)
            return web.json_response({"error": "unavailable"}, status=503)

        resp = web.StreamResponse(
            status=200,
            headers={
                "Content-Type": "application/octet-stream",
                "Cache-Control": "no-store",
            },
        )
        await resp.prepare(self.request)
        try:
            async for chunk in bridge.consume(relay_id):
                await resp.write(chunk)
        finally:
            bridge.close(relay_id)
        await resp.write_eof()
        return resp


class HighlightRelayUploadView(GfsBaseView):
    """``POST /gfs/highlight_rtc/relay-stream/{relay_id}`` — author streams.

    Signed via the header-based :func:`authenticate_relay_stream`; the
    request body is the raw framed byte stream. We feed it chunk-by-chunk
    into the bridge channel the guest GET is draining. Authority guard:
    the relay's target instance must equal the authenticated signer.
    """

    async def post(self) -> web.Response:
        result = await authenticate_relay_stream(self)
        if isinstance(result, web.Response):
            return result
        instance_id = result
        relay_id = self.match("relay_id")
        bridge = self.svc(K.gfs_relay_bridge_key)
        channel = bridge.get(relay_id)
        if channel is None:
            return web.json_response({"error": "not_found"}, status=404)
        if channel.target_instance_id != instance_id:
            return web.json_response({"error": "forbidden"}, status=403)
        try:
            async for chunk in self.request.content.iter_chunked(
                RELAY_READ_CHUNK_BYTES
            ):
                if not await bridge.feed(relay_id, chunk):
                    # Guest hung up — stop reading the upload early.
                    break
        finally:
            await bridge.finish(relay_id)
        return web.json_response({"status": "ok"})


# ─── Author surface (signed) ─────────────────────────────────────────────


class HighlightRtcAnswerView(GfsBaseView):
    """``POST /gfs/highlight_rtc/answer`` — author returns the SDP answer.

    Body: ``{instance_id, session_id, sdp, signature}`` — signed with
    the author SH's Ed25519 key, verified by the shared
    :func:`_rtc_authenticate` middleware.
    """

    async def post(self) -> web.Response:
        result = await _rtc_authenticate(self)
        if isinstance(result, web.Response):
            return result
        body, instance_id = result
        session_id = str(body.get("session_id") or "")
        sdp = str(body.get("sdp") or "")
        if not session_id or not sdp:
            return web.json_response({"error": "missing_fields"}, status=422)
        rtc = self.svc(K.gfs_rtc_key)
        session = rtc.get_session(session_id)
        if session is None:
            return web.json_response({"error": "not_found"}, status=404)
        # Authority guard: only the instance the offer was pushed to
        # may answer this session.
        if session.initiator_id != instance_id:
            return web.json_response({"error": "forbidden"}, status=403)
        await rtc.answer(session_id, sdp)
        return web.json_response({"status": "ok"})


class HighlightRtcAuthorIceView(GfsBaseView):
    """``POST /gfs/highlight_rtc/ice/author`` — author pushes an ICE candidate
    back to the GFS so the viewer's poll picks it up."""

    async def post(self) -> web.Response:
        result = await _rtc_authenticate(self)
        if isinstance(result, web.Response):
            return result
        body, instance_id = result
        session_id = str(body.get("session_id") or "")
        candidate = body.get("candidate") or {}
        if not session_id or not isinstance(candidate, dict):
            return web.json_response({"error": "invalid"}, status=422)
        rtc = self.svc(K.gfs_rtc_key)
        session = rtc.get_session(session_id)
        if session is None:
            return web.json_response({"error": "not_found"}, status=404)
        if session.initiator_id != instance_id:
            return web.json_response({"error": "forbidden"}, status=403)
        await rtc.ice_candidate(session_id, candidate)
        return web.json_response({"status": "ok"})


# ─── ICE servers helper ─────────────────────────────────────────────────


class HighlightIceServersView(GfsBaseView):
    """``GET /gfs/highlights/ice-servers`` — public list of STUN/TURN URLs.

    The vanilla-TS bootstrap fetches this on first paint so it can
    build the ``RTCPeerConnection`` config. We expose only what's safe
    for an anonymous viewer: STUN servers, and TURN URLs with
    short-lived credentials when the operator configured them.
    """

    async def get(self) -> web.Response:
        config = self.svc(K.gfs_config_key)
        servers: list[dict] = []
        stun = (
            getattr(config, "ice_stun_url", "").strip()
            or "stun:stun.l.google.com:19302"
        )
        servers.append({"urls": [stun]})
        turn_url = getattr(config, "ice_turn_url", "")
        if turn_url:
            entry: dict = {"urls": [turn_url]}
            user = getattr(config, "ice_turn_user", "")
            cred = getattr(config, "ice_turn_credential", "")
            if user:
                entry["username"] = user
            if cred:
                entry["credential"] = cred
            servers.append(entry)
        return web.json_response({"servers": servers})
