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

import logging

from aiohttp import web

from .. import app_keys as K
from .base import GfsBaseView
from .rtc import _rtc_authenticate

log = logging.getLogger(__name__)


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
