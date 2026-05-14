"""Conversation (DM) routes — /api/conversations/* (section 23.47)."""

from __future__ import annotations

from aiohttp import web

from ..app_keys import (
    conversation_repo_key,
    dm_service_key,
    media_signer_key,
    notification_service_key,
    online_status_service_key,
    user_repo_key,
)
from ..media_signer import sign_media_urls_in, strip_signature_query
from ..security import error_response, sanitise_for_api
from .base import BaseView


class ConversationCollectionView(BaseView):
    """GET /api/conversations — list conversations for the current user."""

    async def get(self) -> web.Response:
        ctx = self.user
        svc = self.svc(dm_service_key)
        repo = self.svc(conversation_repo_key)
        user_repo = self.svc(user_repo_key)
        convos = await svc.list_conversations(ctx.username)

        # Fold a small member preview into each row so the inbox can
        # render avatar stacks + a peer-name fallback ("Anna, Bob")
        # without N+1 follow-up fetches. Self is filtered out so the
        # preview reads as "the others".
        rows: list[dict] = []
        for c in convos:
            members = await repo.list_members(c.id)
            preview: list[dict] = []
            own_last_read_at: str | None = None
            for m in members:
                if m.username == ctx.username:
                    # Stash the caller's own read watermark so the SPA can
                    # render a "New messages since you last looked" divider
                    # without a second round-trip.
                    own_last_read_at = m.last_read_at
                    continue
                u = await user_repo.get(m.username)
                if u is None:
                    continue
                preview.append(
                    {
                        "user_id": u.user_id,
                        "username": u.username,
                        "display_name": u.display_name,
                        "picture_url": (
                            f"api/users/{u.user_id}/picture?v={u.picture_hash}"
                            if u.picture_hash
                            else None
                        ),
                    }
                )
            unread = await svc.count_unread(c.id, username=ctx.username)
            rows.append(
                {
                    "id": c.id,
                    "type": c.type.value,
                    "name": c.name,
                    "last_message_at": c.last_message_at.isoformat()
                    if c.last_message_at
                    else None,
                    "members": preview,
                    "member_count": len(members),
                    "unread": unread,
                    # ISO 8601 timestamp the caller last marked-as-read on
                    # this conversation. ``null`` for brand-new threads.
                    # The SPA uses this to find the first-unread message
                    # in the loaded window and scroll to the "New
                    # messages" divider on entry.
                    "last_read_at": own_last_read_at,
                }
            )
        return web.json_response(rows)


class ConversationDmView(BaseView):
    """POST /api/conversations/dm — create a 1:1 DM.

    Body shape: either ``{"username": "..."}`` (local-only DM) or
    ``{"user_id": "..."}`` (cross-household DM with a remote user
    already mirrored from a paired peer's directory).
    """

    async def post(self) -> web.Response:
        ctx = self.user
        svc = self.svc(dm_service_key)
        body = await self.body()
        username = body.get("username")
        user_id = body.get("user_id")
        if (username is None) == (user_id is None):
            return error_response(
                422,
                "UNPROCESSABLE",
                "exactly one of username / user_id is required",
            )
        conv = await svc.create_dm(
            creator_username=ctx.username,
            other_username=username,
            other_user_id=user_id,
        )
        return web.json_response({"id": conv.id, "type": conv.type.value}, status=201)


class ConversationGroupView(BaseView):
    """POST /api/conversations/group — create a group DM."""

    async def post(self) -> web.Response:
        ctx = self.user
        svc = self.svc(dm_service_key)
        body = await self.body()
        conv = await svc.create_group_dm(
            creator_username=ctx.username,
            member_usernames=body.get("members", []),
            name=body.get("name"),
        )
        return web.json_response({"id": conv.id, "type": conv.type.value}, status=201)


class ConversationMessageView(BaseView):
    """GET/POST /api/conversations/{id}/messages — list or send messages."""

    async def get(self) -> web.Response:
        ctx = self.user
        svc = self.svc(dm_service_key)
        conv_id = self.match("id")
        before = self.request.query.get("before")
        limit = min(max(int(self.request.query.get("limit", 50)), 1), 100)
        msgs = await svc.list_messages(
            conv_id,
            reader_username=ctx.username,
            before=before,
            limit=limit,
        )
        signer = self.request.app.get(media_signer_key)
        payload = [
            sanitise_for_api(
                {
                    "id": m.id,
                    "sender_user_id": m.sender_user_id,
                    "content": m.content,
                    "type": m.type,
                    "media_url": m.media_url,
                    "reply_to_id": m.reply_to_id,
                    "deleted": m.deleted,
                    "created_at": m.created_at.isoformat() if m.created_at else None,
                    "edited_at": m.edited_at.isoformat() if m.edited_at else None,
                }
            )
            for m in msgs
        ]
        if signer is not None:
            sign_media_urls_in(payload, signer)
        return web.json_response(payload)

    async def post(self) -> web.Response:
        ctx = self.user
        svc = self.svc(dm_service_key)
        conv_id = self.match("id")
        body = await self.body()
        msg = await svc.send_message(
            conv_id,
            sender_username=ctx.username,
            content=body.get("content", ""),
            type=body.get("type", "text"),
            media_url=strip_signature_query(body.get("media_url")),
            reply_to_id=body.get("reply_to_id"),
        )
        return web.json_response({"id": msg.id}, status=201)


class ConversationMembersView(BaseView):
    """``GET /api/conversations/{id}/members`` — roster for one DM.

    Each row carries ``user_id``, ``username``, ``display_name``,
    plus the same session-presence triple as ``GET /api/presence``
    (``is_online`` / ``is_idle`` / ``last_seen_at``) so the DM thread
    header can render a WhatsApp-style "Online" / "Last seen 2 h ago"
    status without a follow-up fetch.
    """

    async def get(self) -> web.Response:
        ctx = self.user
        conv_id = self.match("id")
        repo = self.svc(conversation_repo_key)
        members = await repo.list_members(conv_id)
        # Member rows hold ``username``; the user_repo lookup gives us
        # display_name + user_id (and the persisted last_seen_at fallback
        # for offline users).
        user_repo = self.svc(user_repo_key)
        online_svc = self.request.app.get(online_status_service_key)
        rows: list[dict] = []
        for m in members:
            u = await user_repo.get(m.username)
            if u is None:
                continue
            is_online = bool(online_svc and online_svc.is_online(u.user_id))
            is_idle = bool(online_svc and online_svc.is_idle(u.user_id))
            if is_online and online_svc is not None:
                last_dt = online_svc.last_seen(u.user_id)
                last_seen = last_dt.isoformat() if last_dt is not None else None
            else:
                last_seen = u.last_seen_at
            rows.append(
                {
                    "user_id": u.user_id,
                    "username": u.username,
                    "display_name": u.display_name,
                    "is_self": ctx is not None and ctx.user_id == u.user_id,
                    "is_online": is_online,
                    "is_idle": is_idle,
                    "last_seen_at": last_seen,
                }
            )
        return self._json(rows)


class ConversationReadView(BaseView):
    """POST /api/conversations/{id}/read — mark conversation as read.

    Updates the caller's watermark AND bulk-upserts
    ``conversation_delivery_state`` rows so other participants see the
    read-receipt tick. Returns ``{marked}`` — count of messages whose
    state flipped to ``read``.
    """

    async def post(self) -> web.Response:
        ctx = self.user
        svc = self.svc(dm_service_key)
        conv_id = self.match("id")
        marked = await svc.mark_read(conv_id, username=ctx.username)
        # Clear bell badges for this conversation in lockstep with the
        # read-receipt update — opening a thread is the natural "I've
        # seen these" signal, no separate UI gesture needed.
        notif_svc = self.request.app.get(notification_service_key)
        if notif_svc is not None and ctx is not None:
            try:
                await notif_svc.mark_read_for_dm(ctx.user_id, conv_id)
            except Exception:
                # Best-effort — never block the read receipt on this.
                pass
        return web.json_response({"ok": True, "marked": int(marked or 0)})


class ConversationUnreadView(BaseView):
    """GET /api/conversations/{id}/unread — unread message count."""

    async def get(self) -> web.Response:
        ctx = self.user
        svc = self.svc(dm_service_key)
        conv_id = self.match("id")
        count = await svc.count_unread(conv_id, username=ctx.username)
        return web.json_response({"unread": count})


class ConversationMessageDeliveryView(BaseView):
    """``POST /api/conversations/{id}/messages/{mid}/delivered`` — stamp
    the caller's delivery state for one message.

    Called by the client when a DM_MESSAGE WebSocket frame lands or the
    message first appears in a list response. Idempotent; a later
    ``mark_read`` of the whole conversation supersedes.
    """

    async def post(self) -> web.Response:
        ctx = self.user
        svc = self.svc(dm_service_key)
        conv_id = self.match("id")
        message_id = self.match("mid")
        await svc.mark_delivered(
            conv_id,
            message_id=message_id,
            username=ctx.username,
        )
        return web.json_response({"ok": True})


class ConversationDeliveryStatesView(BaseView):
    """``GET /api/conversations/{id}/delivery-states`` — bulk read.

    Returns one row per (message, user) so the client can render
    checkmarks. Optional ``?message_ids=a,b,c`` narrows the query.
    """

    async def get(self) -> web.Response:
        ctx = self.user
        svc = self.svc(dm_service_key)
        conv_id = self.match("id")
        raw_ids = self.request.query.get("message_ids") or ""
        ids = [x for x in raw_ids.split(",") if x] or None
        states = await svc.list_delivery_states(
            conv_id,
            username=ctx.username,
            message_ids=ids,
        )
        return web.json_response({"states": states})


class ConversationGapsView(BaseView):
    """``GET /api/conversations/{id}/gaps`` — open sequence holes.

    Returns one row per (sender, expected_seq) pair the inbound
    validator flagged as missing. Used by the client to surface a
    "messages may be missing" banner.
    """

    async def get(self) -> web.Response:
        ctx = self.user
        svc = self.svc(dm_service_key)
        conv_id = self.match("id")
        gaps = await svc.list_open_gaps(conv_id, username=ctx.username)
        return web.json_response({"gaps": gaps})
