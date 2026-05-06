"""Federation inbound service — land remote DM/space/user events locally (§24).

The §24.11 validation pipeline (``federation/inbound_validator.py``) has
already verified signature, replay cache, ban list, and decrypted the
payload by the time an event reaches the event registry. Handlers
attached by this service persist the effect locally and publish the
matching :class:`DomainEvent` on the bus so
:class:`~socialhome.services.realtime_service.RealtimeService` can
fan out to WebSocket clients.

Events without a concrete subscriber fall through to a debug log — the
event dispatch registry never raises, so silent drops are observable.
"""

from __future__ import annotations

import base64
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from ..domain.conversation import (
    ConversationMessage,
    MESSAGE_TYPES,
)
from ..domain.events import (
    CommentAdded,
    CommentDeleted,
    CommentUpdated,
    DmMessageCreated,
    MomentCreated,
    MomentDeleted,
    MomentReactionChanged,
    PostDeleted,
    SpaceMemberProfileUpdated,
    SpacePostCreated,
    HighlightFrameAdded,
    HighlightFrameReactionChanged,
    HighlightFrameRemoved,
    HighlightFrameViewed,
    HighlightRemoved,
    UserStatusChanged,
)
from ..domain.post import Comment, CommentType, LocationData, Post, PostType
from ..domain.space import SpaceMember
from ..domain.highlight import (
    Highlight,
    HighlightAudience,
    HighlightFrame,
    HighlightFrameType,
)
from ..domain.user import RemoteUser, UserStatus
from ..infrastructure.event_bus import EventBus
from ..media.image_processor import ImageProcessor
from ..repositories.profile_picture_repo import compute_picture_hash
from ..services.user_service import PROFILE_PICTURE_MAX_DIMENSION
from ..utils.datetime import parse_iso8601_lenient

if TYPE_CHECKING:
    from ..domain.federation import FederationEvent
    from ..repositories.conversation_repo import AbstractConversationRepo
    from ..repositories.moment_repo import AbstractMomentRepo
    from ..repositories.space_post_repo import AbstractSpacePostRepo
    from ..repositories.space_repo import AbstractSpaceRepo
    from ..repositories.highlight_repo import AbstractHighlightRepo
    from ..repositories.user_repo import AbstractUserRepo
    from .moment_federation_outbound import MomentFederationOutbound

log = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class FederationInboundService:
    """Apply decrypted inbound federation events to local state.

    Registers handlers for the event families backed by a concrete repo:
    DM messages, space posts/comments, space membership, user status.
    Handlers call the injected repos to persist the row and publish a
    local :class:`DomainEvent` so the realtime layer picks it up.
    """

    __slots__ = (
        "_bus",
        "_conversation_repo",
        "_space_post_repo",
        "_space_repo",
        "_user_repo",
        "_highlight_repo",
        "_moment_repo",
        "_moment_outbound",
        "_profile_picture_repo",
        "_report_service",
        "_dm_routing_repo",
    )

    def __init__(
        self,
        *,
        bus: EventBus,
        conversation_repo: "AbstractConversationRepo",
        space_post_repo: "AbstractSpacePostRepo",
        space_repo: "AbstractSpaceRepo",
        user_repo: "AbstractUserRepo",
        highlight_repo: "AbstractHighlightRepo | None" = None,
        moment_repo: "AbstractMomentRepo | None" = None,
        moment_outbound: "MomentFederationOutbound | None" = None,
        profile_picture_repo=None,
        report_service=None,
        dm_routing_repo=None,
    ) -> None:
        self._bus = bus
        self._conversation_repo = conversation_repo
        self._space_post_repo = space_post_repo
        self._space_repo = space_repo
        self._user_repo = user_repo
        self._highlight_repo = highlight_repo
        self._moment_repo = moment_repo
        self._moment_outbound = moment_outbound
        self._profile_picture_repo = profile_picture_repo
        self._report_service = report_service
        self._dm_routing_repo = dm_routing_repo

    def attach_to(self, federation_service) -> None:
        """Register inbound handlers on the federation event registry."""
        from ..domain.federation import FederationEventType as FET

        registry = federation_service._event_registry
        registry.register(FET.DM_MESSAGE, self._on_dm_message)
        registry.register(FET.DM_MESSAGE_DELETED, self._on_dm_deleted)
        registry.register(FET.DM_MESSAGE_REACTION, self._on_dm_reaction)

        registry.register(FET.SPACE_POST_CREATED, self._on_space_post_created)
        registry.register(FET.SPACE_POST_UPDATED, self._on_space_post_updated)
        registry.register(FET.SPACE_POST_DELETED, self._on_space_post_deleted)
        registry.register(FET.SPACE_COMMENT_CREATED, self._on_space_comment_added)
        registry.register(FET.SPACE_COMMENT_UPDATED, self._on_space_comment_updated)
        registry.register(FET.SPACE_COMMENT_DELETED, self._on_space_comment_deleted)

        registry.register(FET.SPACE_MEMBER_JOINED, self._on_space_member_joined)
        registry.register(FET.SPACE_MEMBER_LEFT, self._on_space_member_left)
        registry.register(
            FET.SPACE_MEMBER_PROFILE_UPDATED,
            self._on_space_member_profile_updated,
        )

        registry.register(FET.USERS_SYNC, self._on_users_sync)
        registry.register(FET.USER_UPDATED, self._on_user_updated)
        registry.register(FET.USER_REMOVED, self._on_user_removed)
        registry.register(FET.USER_STATUS_UPDATED, self._on_user_status_updated)

        registry.register(FET.SPACE_REPORT, self._on_space_report)

        # Highlights — only registered when a highlight repo is wired in. Tests
        # that instantiate :class:`FederationInboundService` for non-
        # highlight coverage don't need to plumb a highlight repo through.
        if self._highlight_repo is not None:
            registry.register(FET.HIGHLIGHT_CREATED, self._on_highlight_created)
            registry.register(
                FET.HIGHLIGHT_FRAME_APPENDED, self._on_highlight_frame_appended
            )
            registry.register(
                FET.HIGHLIGHT_FRAME_DELETED, self._on_highlight_frame_deleted
            )
            registry.register(FET.HIGHLIGHT_DELETED, self._on_highlight_deleted)
            registry.register(
                FET.HIGHLIGHT_FRAME_VIEWED, self._on_highlight_frame_viewed
            )
            registry.register(
                FET.HIGHLIGHT_FRAME_REACTED, self._on_highlight_frame_reacted
            )
            registry.register(
                FET.HIGHLIGHT_FRAME_REACTION_REMOVED,
                self._on_highlight_frame_reaction_removed,
            )

        # Moments — only registered when the moment repo is wired.
        if self._moment_repo is not None:
            registry.register(FET.MOMENT_CREATED, self._on_moment_created)
            registry.register(FET.MOMENT_DELETED, self._on_moment_deleted)
            registry.register(FET.MOMENT_REACTED, self._on_moment_reacted)
            registry.register(
                FET.MOMENT_REACTION_REMOVED,
                self._on_moment_reaction_removed,
            )

    # ── DM handlers ────────────────────────────────────────────────────

    async def _on_dm_message(self, event: "FederationEvent") -> None:
        p = event.payload
        conv_id = str(p.get("conversation_id") or "")
        message_id = str(p.get("message_id") or "")
        sender_user_id = str(p.get("sender_user_id") or "")
        content = str(p.get("content") or "")
        msg_type = str(p.get("type") or "text")
        if not conv_id or not message_id or not sender_user_id:
            log.debug("DM_MESSAGE missing required field: %s", p)
            return
        if msg_type not in MESSAGE_TYPES:
            msg_type = "text"

        # §12.5 gap detection — when the sender stamps a monotonic
        # ``sender_seq`` on the envelope, compare against our last-seen
        # value and persist one ``conversation_message_gaps`` row per
        # missing sequence. Skipped when the routing repo isn't wired
        # or the payload doesn't carry a seq (backwards-compat path).
        sender_seq = p.get("sender_seq")
        if self._dm_routing_repo is not None and sender_seq is not None:
            try:
                incoming = int(sender_seq)
            except TypeError, ValueError:
                incoming = 0
            if incoming > 0:
                last = await self._dm_routing_repo.peek_sender_seq(
                    conversation_id=conv_id,
                    sender_user_id=sender_user_id,
                )
                if incoming > last + 1:
                    missing = list(range(last + 1, incoming))
                    log.warning(
                        "DM gap detected conv=%s sender=%s missing=%d..%d",
                        conv_id,
                        sender_user_id,
                        missing[0],
                        missing[-1],
                    )
                    await self._dm_routing_repo.insert_gaps(
                        conversation_id=conv_id,
                        sender_user_id=sender_user_id,
                        expected_seqs=missing,
                    )
                elif incoming <= last:
                    # Out-of-order delivery resolving a previously-
                    # recorded gap; clear it so the UI banner disappears.
                    await self._dm_routing_repo.resolve_gap(
                        conversation_id=conv_id,
                        sender_user_id=sender_user_id,
                        expected_seq=incoming,
                    )

        msg = ConversationMessage(
            id=message_id,
            conversation_id=conv_id,
            sender_user_id=sender_user_id,
            content=content,
            created_at=parse_iso8601_lenient(p.get("occurred_at")),
            type=msg_type,
            media_url=p.get("media_url"),
        )
        await self._conversation_repo.save_message(msg)

        recipients = tuple(p.get("recipient_user_ids") or ())
        await self._bus.publish(
            DmMessageCreated(
                conversation_id=conv_id,
                message_id=message_id,
                sender_user_id=sender_user_id,
                sender_display_name=str(p.get("sender_display_name") or sender_user_id),
                recipient_user_ids=tuple(str(r) for r in recipients),
                content=content,
                message_type=msg_type,
                media_url=p.get("media_url"),
                reply_to_id=p.get("reply_to_id"),
                occurred_at=msg.created_at,
            )
        )

    async def _on_dm_deleted(self, event: "FederationEvent") -> None:
        message_id = str(event.payload.get("message_id") or "")
        if not message_id:
            return
        await self._conversation_repo.soft_delete_message(message_id)

    async def _on_dm_reaction(self, event: "FederationEvent") -> None:
        p = event.payload
        message_id = str(p.get("message_id") or "")
        user_id = str(p.get("user_id") or "")
        emoji = str(p.get("emoji") or "")
        action = str(p.get("action") or "add")
        if not message_id or not user_id or not emoji:
            return
        if action == "remove":
            await self._conversation_repo.remove_reaction(message_id, user_id, emoji)
        else:
            await self._conversation_repo.add_reaction(message_id, user_id, emoji)

    # ── Space content handlers ─────────────────────────────────────────

    async def _on_space_post_created(self, event: "FederationEvent") -> None:
        space_id = event.space_id or str(event.payload.get("space_id") or "")
        if not space_id:
            return
        post = self._post_from_payload(event.payload)
        if post is None:
            return
        await self._space_post_repo.save(space_id, post)
        await self._bus.publish(SpacePostCreated(post=post, space_id=space_id))

    async def _on_space_post_updated(self, event: "FederationEvent") -> None:
        p = event.payload
        post_id = str(p.get("id") or p.get("post_id") or "")
        new_content = str(p.get("content") or "")
        if not post_id:
            return
        await self._space_post_repo.edit(post_id, new_content)

    async def _on_space_post_deleted(self, event: "FederationEvent") -> None:
        post_id = str(event.payload.get("post_id") or event.payload.get("id") or "")
        if not post_id:
            return
        moderated_by = event.payload.get("moderated_by")
        await self._space_post_repo.soft_delete(
            post_id,
            moderated_by=str(moderated_by) if moderated_by else None,
        )
        await self._bus.publish(PostDeleted(post_id=post_id))

    async def _on_space_comment_added(self, event: "FederationEvent") -> None:
        p = event.payload
        post_id = str(p.get("post_id") or "")
        comment_id = str(p.get("comment_id") or p.get("id") or "")
        author = str(p.get("author") or "")
        if not post_id or not comment_id or not author:
            return
        comment_type_str = str(p.get("type") or "text")
        try:
            comment_type = CommentType(comment_type_str)
        except ValueError:
            comment_type = CommentType.TEXT
        comment = Comment(
            id=comment_id,
            post_id=post_id,
            author=author,
            type=comment_type,
            created_at=parse_iso8601_lenient(p.get("occurred_at")),
            parent_id=p.get("parent_id"),
            content=p.get("content") or "",
            media_url=p.get("media_url"),
        )
        await self._space_post_repo.add_comment(comment)
        await self._space_post_repo.increment_comment_count(post_id)
        await self._bus.publish(
            CommentAdded(
                post_id=post_id,
                comment=comment,
                space_id=str(p.get("space_id") or event.space_id or "") or None,
            ),
        )

    async def _on_space_comment_updated(self, event: "FederationEvent") -> None:
        p = event.payload
        comment_id = str(p.get("id") or p.get("comment_id") or "")
        content = p.get("content")
        if not comment_id or content is None:
            return
        await self._space_post_repo.edit_comment(comment_id, str(content))
        refreshed = await self._space_post_repo.get_comment(comment_id)
        if refreshed is None:
            return
        await self._bus.publish(
            CommentUpdated(
                post_id=refreshed.post_id,
                comment=refreshed,
                space_id=str(p.get("space_id") or event.space_id or "") or None,
            ),
        )

    async def _on_space_comment_deleted(self, event: "FederationEvent") -> None:
        p = event.payload
        comment_id = str(p.get("comment_id") or p.get("id") or "")
        post_id = str(p.get("post_id") or "")
        if not comment_id or not post_id:
            return
        await self._space_post_repo.soft_delete_comment(comment_id)
        await self._space_post_repo.decrement_comment_count(post_id)
        await self._bus.publish(
            CommentDeleted(
                post_id=post_id,
                comment_id=comment_id,
                space_id=str(p.get("space_id") or event.space_id or "") or None,
            ),
        )

    # ── Report handler ─────────────────────────────────────────────────

    async def _on_space_report(self, event: "FederationEvent") -> None:
        """A peer's member reported content we host — persist locally."""
        if self._report_service is None:
            log.debug("SPACE_REPORT received but no ReportService attached")
            return
        p = event.payload
        await self._report_service.create_report_from_remote(
            reporter_user_id=str(p.get("reporter_user_id") or ""),
            reporter_instance_id=event.from_instance,
            target_type=str(p.get("target_type") or ""),
            target_id=str(p.get("target_id") or ""),
            category=str(p.get("category") or ""),
            notes=p.get("notes"),
        )

    # ── Space membership handlers ──────────────────────────────────────

    async def _on_space_member_joined(self, event: "FederationEvent") -> None:
        space_id = event.space_id or str(event.payload.get("space_id") or "")
        user_id = str(event.payload.get("user_id") or "")
        if not space_id or not user_id:
            return
        role = str(event.payload.get("role") or "member")
        joined_at = event.payload.get("occurred_at") or _now_iso()
        member = SpaceMember(
            space_id=space_id,
            user_id=user_id,
            role=role,
            joined_at=str(joined_at),
        )
        await self._space_repo.save_member(member)

    async def _on_space_member_left(self, event: "FederationEvent") -> None:
        space_id = event.space_id or str(event.payload.get("space_id") or "")
        user_id = str(event.payload.get("user_id") or "")
        if not space_id or not user_id:
            return
        await self._space_repo.delete_member(space_id, user_id)

    async def _on_space_member_profile_updated(
        self,
        event: "FederationEvent",
    ) -> None:
        p = event.payload
        space_id = event.space_id or str(p.get("space_id") or "")
        user_id = str(p.get("user_id") or "")
        if not space_id or not user_id:
            return
        member = await self._space_repo.get_member(space_id, user_id)
        if member is None:
            # Unknown member on this side — skip silently; a membership
            # event will catch up eventually.
            return
        picture_hash = p.get("picture_hash")
        bytes_b64 = p.get("picture_webp_base64")
        if bytes_b64 and self._profile_picture_repo is not None:
            try:
                raw = base64.b64decode(bytes_b64)
                webp = await ImageProcessor().generate_thumbnail(
                    raw,
                    size=PROFILE_PICTURE_MAX_DIMENSION,
                )
                local_hash = compute_picture_hash(webp)
                await self._profile_picture_repo.set_member_picture(
                    space_id,
                    user_id,
                    bytes_webp=webp,
                    hash=local_hash,
                    width=PROFILE_PICTURE_MAX_DIMENSION,
                    height=PROFILE_PICTURE_MAX_DIMENSION,
                )
                picture_hash = local_hash
            except Exception as exc:  # pragma: no cover — defensive
                log.warning(
                    "SPACE_MEMBER_PROFILE_UPDATED: bad blob for %s in %s: %s",
                    user_id,
                    space_id,
                    exc,
                )
        await self._space_repo.set_member_profile(
            space_id,
            user_id,
            space_display_name=p.get("space_display_name"),
            picture_hash=picture_hash,
        )
        await self._bus.publish(
            SpaceMemberProfileUpdated(
                space_id=space_id,
                user_id=user_id,
                space_display_name=p.get("space_display_name"),
                picture_hash=picture_hash,
            )
        )

    # ── User-profile handlers ──────────────────────────────────────────

    async def _on_users_sync(self, event: "FederationEvent") -> None:
        users = event.payload.get("users") or []
        if not isinstance(users, list):
            return
        for u in users:
            await self._upsert_remote_user(event.from_instance, u)

    async def _on_user_updated(self, event: "FederationEvent") -> None:
        await self._upsert_remote_user(event.from_instance, event.payload)

    async def _on_user_removed(self, event: "FederationEvent") -> None:
        """Mark a remote user as deprovisioned locally.

        The row stays in ``remote_users`` so historical posts / comments
        keep resolving to a display name, but member-list and
        autocomplete queries filter it out via
        ``list_remote_for_instance``.
        """
        user_id = str(event.payload.get("user_id") or "")
        if not user_id:
            return
        log.info("USER_REMOVED: flagging remote user %s as deprovisioned", user_id)
        await self._user_repo.mark_remote_deprovisioned(user_id)

    async def _on_user_status_updated(self, event: "FederationEvent") -> None:
        p = event.payload
        user_id = str(p.get("user_id") or "")
        if not user_id:
            return
        status: UserStatus | None
        if p.get("status_cleared"):
            status = None
        else:
            emoji = p.get("emoji")
            text = p.get("text")
            if emoji is None and text is None:
                status = None
            else:
                status = UserStatus(
                    emoji=str(emoji) if emoji else None,
                    text=str(text) if text else None,
                    expires_at=str(p["expires_at"]) if p.get("expires_at") else None,
                )
        await self._bus.publish(UserStatusChanged(user_id=user_id, status=status))

    # ── Highlight handlers ─────────────────────────────────────────────────

    async def _on_highlight_created(self, event: "FederationEvent") -> None:
        """Land a remote ``HIGHLIGHT_CREATED`` envelope.

        Persists the parent ``Highlight`` row (or upserts an existing one
        with refreshed audience/expiry) plus the first frame, then
        republishes :class:`HighlightFrameAdded` so :class:`RealtimeService`
        can fan a ``highlight.frame_added`` WS frame to local viewers.

        Authority check: the envelope's signed sender (``from_instance``)
        must equal the home instance of the payload's
        ``author_user_id`` — peers can't impersonate highlights from
        someone else's instance.
        """
        if self._highlight_repo is None:
            return
        p = event.payload
        highlight = self._highlight_from_payload(p)
        if highlight is None:
            log.debug("HIGHLIGHT_CREATED missing required field: %s", p)
            return
        if not await self._authority_matches(
            event.from_instance, highlight.author_user_id
        ):
            log.warning(
                "HIGHLIGHT_CREATED authority mismatch: envelope from %s, "
                "author %s lives elsewhere — dropped",
                event.from_instance,
                highlight.author_user_id,
            )
            return
        await self._highlight_repo.save_highlight(highlight)
        frame = self._frame_from_payload(highlight.id, p)
        if frame is not None:
            await self._highlight_repo.save_frame(frame)
            await self._publish_frame_added(highlight, frame, is_first=True, p=p)

    async def _on_highlight_frame_appended(self, event: "FederationEvent") -> None:
        """Append a frame to an existing remote highlight.

        We expect the ``HIGHLIGHT_CREATED`` envelope to have arrived first —
        if the parent highlight is missing locally (out-of-order delivery
        or pruned by retention), we lazily upsert it from the same
        payload, since every frame envelope carries the routing fields
        the parent needs.
        """
        if self._highlight_repo is None:
            return
        p = event.payload
        highlight_id = str(p.get("highlight_id") or "")
        if not highlight_id:
            log.debug("HIGHLIGHT_FRAME_APPENDED missing highlight_id: %s", p)
            return
        highlight = await self._highlight_repo.get_highlight(highlight_id)
        if highlight is None:
            highlight = self._highlight_from_payload(p)
            if highlight is None:
                log.debug(
                    "HIGHLIGHT_FRAME_APPENDED for unknown highlight_id %s and no "
                    "fallback metadata — dropped",
                    highlight_id,
                )
                return
            if not await self._authority_matches(
                event.from_instance, highlight.author_user_id
            ):
                log.warning(
                    "HIGHLIGHT_FRAME_APPENDED authority mismatch (lazy parent): "
                    "envelope from %s, author %s lives elsewhere — dropped",
                    event.from_instance,
                    highlight.author_user_id,
                )
                return
            await self._highlight_repo.save_highlight(highlight)
        else:
            if not await self._authority_matches(
                event.from_instance, highlight.author_user_id
            ):
                log.warning(
                    "HIGHLIGHT_FRAME_APPENDED authority mismatch: envelope from "
                    "%s, highlight author %s lives elsewhere — dropped",
                    event.from_instance,
                    highlight.author_user_id,
                )
                return
        frame = self._frame_from_payload(highlight.id, p)
        if frame is None:
            log.debug("HIGHLIGHT_FRAME_APPENDED missing frame fields: %s", p)
            return
        await self._highlight_repo.save_frame(frame)
        await self._publish_frame_added(highlight, frame, is_first=False, p=p)

    async def _on_highlight_frame_deleted(self, event: "FederationEvent") -> None:
        if self._highlight_repo is None:
            return
        p = event.payload
        frame_id = str(p.get("frame_id") or "")
        if not frame_id:
            return
        frame = await self._highlight_repo.get_frame(frame_id)
        highlight_id = (
            frame.highlight_id
            if frame is not None
            else str(p.get("highlight_id") or "")
        )
        highlight = (
            await self._highlight_repo.get_highlight(highlight_id)
            if highlight_id
            else None
        )
        if highlight is not None and not await self._authority_matches(
            event.from_instance, highlight.author_user_id
        ):
            log.warning(
                "HIGHLIGHT_FRAME_DELETED authority mismatch: dropped",
            )
            return
        await self._highlight_repo.delete_frame(frame_id)
        if highlight is not None:
            await self._bus.publish(
                HighlightFrameRemoved(
                    highlight_id=highlight.id,
                    frame_id=frame_id,
                    author_user_id=highlight.author_user_id,
                    audience_kind=highlight.audience_kind.value,
                    audience=highlight.audience,
                )
            )

    async def _on_highlight_deleted(self, event: "FederationEvent") -> None:
        if self._highlight_repo is None:
            return
        p = event.payload
        highlight_id = str(p.get("highlight_id") or "")
        if not highlight_id:
            return
        highlight = await self._highlight_repo.get_highlight(highlight_id)
        if highlight is None:
            return
        if not await self._authority_matches(
            event.from_instance, highlight.author_user_id
        ):
            log.warning("HIGHLIGHT_DELETED authority mismatch: dropped")
            return
        await self._highlight_repo.delete_highlight(highlight_id)
        await self._bus.publish(
            HighlightRemoved(
                highlight_id=highlight_id,
                author_user_id=highlight.author_user_id,
                audience_kind=highlight.audience_kind.value,
                audience=highlight.audience,
            )
        )

    # ── Highlight back-channel handlers ────────────────────────────────────

    async def _on_highlight_frame_viewed(self, event: "FederationEvent") -> None:
        """A remote viewer marked one of *our* author's frames as seen.

        Persists the row in ``highlight_frame_views`` and republishes
        :class:`HighlightFrameViewed` so the realtime layer pushes the
        view-count update to the author's WS sessions.
        """
        if self._highlight_repo is None:
            return
        p = event.payload
        highlight_id = str(p.get("highlight_id") or "")
        frame_id = str(p.get("frame_id") or "")
        viewer_user_id = str(p.get("viewer_user_id") or "")
        author_user_id = str(p.get("author_user_id") or "")
        if not (highlight_id and frame_id and viewer_user_id and author_user_id):
            return
        # Authority check: the envelope's signed sender must be the
        # viewer's home instance — peers can't fabricate views from a
        # user that doesn't live on their household.
        if not await self._authority_matches(event.from_instance, viewer_user_id):
            log.warning(
                "HIGHLIGHT_FRAME_VIEWED authority mismatch — dropped",
            )
            return
        await self._highlight_repo.mark_viewed(frame_id, viewer_user_id)
        await self._bus.publish(
            HighlightFrameViewed(
                highlight_id=highlight_id,
                frame_id=frame_id,
                viewer_user_id=viewer_user_id,
                author_user_id=author_user_id,
            )
        )

    async def _on_highlight_frame_reacted(self, event: "FederationEvent") -> None:
        await self._handle_reaction_envelope(event, removed=False)

    async def _on_highlight_frame_reaction_removed(
        self,
        event: "FederationEvent",
    ) -> None:
        await self._handle_reaction_envelope(event, removed=True)

    async def _handle_reaction_envelope(
        self,
        event: "FederationEvent",
        *,
        removed: bool,
    ) -> None:
        if self._highlight_repo is None:
            return
        p = event.payload
        highlight_id = str(p.get("highlight_id") or "")
        frame_id = str(p.get("frame_id") or "")
        reactor_user_id = str(p.get("reactor_user_id") or "")
        author_user_id = str(p.get("author_user_id") or "")
        emoji = None if removed else (p.get("emoji") or None)
        if not (highlight_id and frame_id and reactor_user_id and author_user_id):
            return
        if not await self._authority_matches(event.from_instance, reactor_user_id):
            log.warning(
                "HIGHLIGHT_FRAME_REACT* authority mismatch — dropped",
            )
            return
        if removed or emoji is None:
            await self._highlight_repo.clear_reaction(frame_id, reactor_user_id)
            published_emoji: str | None = None
        else:
            await self._highlight_repo.set_reaction(frame_id, reactor_user_id, emoji)
            published_emoji = emoji
        await self._bus.publish(
            HighlightFrameReactionChanged(
                highlight_id=highlight_id,
                frame_id=frame_id,
                reactor_user_id=reactor_user_id,
                author_user_id=author_user_id,
                emoji=published_emoji,
            )
        )

    # ── Momentum handlers ──────────────────────────────────────────────

    async def _on_moment_created(self, event: "FederationEvent") -> None:
        """Land a remote moment and fire :class:`MomentCreated`.

        Authority check: the envelope's signed sender (``from_instance``)
        must equal the home of ``payload.author_user_id`` for a top-level
        post. The 3-hop relay re-broadcasts the *original* envelope, so
        when the relay path is in play the receiver verifies against
        ``payload.origin_instance_id`` instead — that's the only field
        that pins the original sender across hops.
        """
        if self._moment_repo is None:
            return
        from ..domain.moment import Moment

        p = event.payload
        moment_id = str(p.get("moment_id") or "")
        author_user_id = str(p.get("author_user_id") or "")
        origin_instance_id = str(p.get("origin_instance_id") or "")
        if not (moment_id and author_user_id and origin_instance_id):
            log.debug("MOMENT_CREATED missing required fields: %s", p)
            return
        if not await self._moment_authority_matches(
            event.from_instance,
            origin_instance_id,
            author_user_id,
        ):
            log.warning("MOMENT_CREATED authority mismatch — dropped")
            return
        media_type = p.get("media_type")
        if media_type not in ("image", "video", None):
            media_type = None
        moment = Moment(
            id=moment_id,
            author_user_id=author_user_id,
            content=str(p.get("content") or ""),
            media_url=p.get("media_url"),
            media_type=media_type,
            duration_ms=(
                int(p["duration_ms"]) if p.get("duration_ms") is not None else None
            ),
            parent_moment_id=p.get("parent_moment_id"),
            origin_instance_id=origin_instance_id,
            created_at=str(p.get("occurred_at") or _now_iso()),
            expires_at=str(p.get("expires_at") or _now_iso()),
        )
        await self._moment_repo.save(moment)
        # Look up the parent's author so the local notification handler
        # can ping them when the inbound moment is a reply.
        parent_author_user_id: str | None = None
        if moment.parent_moment_id is not None:
            parent = await self._moment_repo.get(moment.parent_moment_id)
            if parent is not None:
                parent_author_user_id = parent.author_user_id
        # Bus republish so the realtime layer + downstream listeners
        # see the same shape they'd see on a local write.
        await self._bus.publish(
            MomentCreated(
                moment_id=moment.id,
                author_user_id=moment.author_user_id,
                content=moment.content,
                media_url=moment.media_url,
                media_type=moment.media_type,
                duration_ms=moment.duration_ms,
                parent_moment_id=moment.parent_moment_id,
                parent_author_user_id=parent_author_user_id,
                origin_instance_id=moment.origin_instance_id,
                expires_at=moment.expires_at,
            )
        )
        # 3-hop relay: forward to the rest of *our* paired peers. The
        # outbound's bus subscriber would skip this event (author isn't
        # local), so the relay must be triggered explicitly here.
        await self._maybe_relay(
            event_type=event.event_type,
            payload=p,
            from_instance=event.from_instance,
        )

    async def _on_moment_deleted(self, event: "FederationEvent") -> None:
        if self._moment_repo is None:
            return
        p = event.payload
        moment_id = str(p.get("moment_id") or "")
        author_user_id = str(p.get("author_user_id") or "")
        origin_instance_id = str(p.get("origin_instance_id") or "")
        if not (moment_id and author_user_id and origin_instance_id):
            return
        if not await self._moment_authority_matches(
            event.from_instance,
            origin_instance_id,
            author_user_id,
        ):
            log.warning("MOMENT_DELETED authority mismatch — dropped")
            return
        await self._moment_repo.delete(moment_id)
        await self._bus.publish(
            MomentDeleted(
                moment_id=moment_id,
                author_user_id=author_user_id,
                origin_instance_id=origin_instance_id,
            )
        )
        await self._maybe_relay(
            event_type=event.event_type,
            payload=p,
            from_instance=event.from_instance,
        )

    async def _on_moment_reacted(self, event: "FederationEvent") -> None:
        await self._handle_moment_reaction(event, removed=False)

    async def _on_moment_reaction_removed(
        self,
        event: "FederationEvent",
    ) -> None:
        await self._handle_moment_reaction(event, removed=True)

    async def _handle_moment_reaction(
        self,
        event: "FederationEvent",
        *,
        removed: bool,
    ) -> None:
        if self._moment_repo is None:
            return
        p = event.payload
        moment_id = str(p.get("moment_id") or "")
        reactor_user_id = str(p.get("reactor_user_id") or "")
        author_user_id = str(p.get("author_user_id") or "")
        if not (moment_id and reactor_user_id and author_user_id):
            return
        # Authority: envelope sender == reactor's home instance.
        if not await self._authority_matches(event.from_instance, reactor_user_id):
            log.warning("MOMENT_REACT* authority mismatch — dropped")
            return
        emoji = None if removed else (p.get("emoji") or None)
        if removed or emoji is None:
            await self._moment_repo.clear_reaction(moment_id, reactor_user_id)
            published_emoji: str | None = None
        else:
            await self._moment_repo.set_reaction(
                moment_id,
                reactor_user_id,
                emoji,
            )
            published_emoji = emoji
        await self._bus.publish(
            MomentReactionChanged(
                moment_id=moment_id,
                reactor_user_id=reactor_user_id,
                author_user_id=author_user_id,
                emoji=published_emoji,
            )
        )

    async def _moment_authority_matches(
        self,
        from_instance: str,
        origin_instance_id: str,
        author_user_id: str,
    ) -> bool:
        """Origin-vs-relay authority check.

        On a 1-hop direct delivery, ``from_instance == origin_instance_id``
        and ``origin_instance_id`` should be the author's home instance.
        On a 2/3-hop relay, ``from_instance != origin_instance_id`` —
        we trust the origin field on the payload as long as the
        author's home instance lookup matches. Unknown authors (the
        ``USER_UPDATED`` envelope hasn't landed yet) fall through and
        accept the row.
        """
        if from_instance == origin_instance_id:
            # Direct delivery — also check the author lives there.
            try:
                home = await self._user_repo.get_instance_for_user(author_user_id)
            except Exception:  # pragma: no cover — defensive
                return True
            return home is None or home == origin_instance_id
        # Relay: trust the origin field; sender just relayed.
        try:
            home = await self._user_repo.get_instance_for_user(author_user_id)
        except Exception:  # pragma: no cover — defensive
            return True
        return home is None or home == origin_instance_id

    async def _maybe_relay(
        self,
        *,
        event_type,
        payload: dict,
        from_instance: str,
    ) -> None:
        if self._moment_outbound is None:
            return
        try:
            await self._moment_outbound.relay_inbound(
                event_type=event_type,
                payload=payload,
                from_instance=from_instance,
            )
        except Exception as exc:  # pragma: no cover — defensive
            log.debug("moment-relay failed: %s", exc)

    async def _publish_frame_added(
        self,
        highlight: Highlight,
        frame: HighlightFrame,
        *,
        is_first: bool,
        p: dict,
    ) -> None:
        await self._bus.publish(
            HighlightFrameAdded(
                highlight_id=highlight.id,
                frame_id=frame.id,
                author_user_id=highlight.author_user_id,
                highlight_date=highlight.highlight_date,
                sequence=frame.sequence,
                is_first_frame=is_first,
                audience_kind=highlight.audience_kind.value,
                audience=highlight.audience,
                frame_type=frame.frame_type.value,
                media_url=frame.media_url,
                caption_text=frame.caption_text,
                caption_emoji=frame.caption_emoji,
                duration_ms=frame.duration_ms,
                expires_at=highlight.expires_at or str(p.get("expires_at") or ""),
            )
        )

    @staticmethod
    def _highlight_from_payload(payload: dict) -> Highlight | None:
        highlight_id = str(payload.get("highlight_id") or "")
        author = str(payload.get("author_user_id") or "")
        highlight_date = str(payload.get("highlight_date") or "")
        if not highlight_id or not author or not highlight_date:
            return None
        try:
            kind = HighlightAudience(str(payload.get("audience_kind") or "all_paired"))
        except ValueError:
            kind = HighlightAudience.ALL_PAIRED
        audience = tuple(str(x) for x in (payload.get("audience") or ()))
        return Highlight(
            id=highlight_id,
            author_user_id=author,
            highlight_date=highlight_date,
            audience_kind=kind,
            audience=audience,
            expires_at=str(payload.get("expires_at") or "") or None,
        )

    @staticmethod
    def _frame_from_payload(highlight_id: str, payload: dict) -> HighlightFrame | None:
        frame_id = str(payload.get("frame_id") or "")
        if not frame_id:
            return None
        try:
            ftype = HighlightFrameType(str(payload.get("frame_type") or "image"))
        except ValueError:
            ftype = HighlightFrameType.IMAGE
        try:
            sequence = int(payload.get("sequence") or 1)
        except TypeError, ValueError:
            sequence = 1
        try:
            duration = (
                int(payload["duration_ms"])
                if payload.get("duration_ms") is not None
                else None
            )
        except TypeError, ValueError:
            duration = None
        media_url = str(payload.get("media_url") or "")
        if not media_url:
            return None
        return HighlightFrame(
            id=frame_id,
            highlight_id=highlight_id,
            sequence=sequence,
            frame_type=ftype,
            media_url=media_url,
            caption_text=payload.get("caption_text"),
            caption_emoji=payload.get("caption_emoji"),
            duration_ms=duration,
        )

    async def _authority_matches(
        self,
        from_instance: str,
        author_user_id: str,
    ) -> bool:
        """Reject envelopes that claim authorship for a user not on the
        sending instance. Mismatches are logged + dropped so a misbehaved
        peer can't plant content on the audience's behalf."""
        try:
            home = await self._user_repo.get_instance_for_user(author_user_id)
        except Exception as exc:  # pragma: no cover — defensive
            log.debug("highlight authority lookup failed: %s", exc)
            return False
        # If the author is unknown locally, accept on first sight — the
        # ``USER_UPDATED`` / ``USERS_SYNC`` envelope from the same peer
        # will arrive eventually and seed ``remote_users``.
        if home is None:
            return True
        return home == from_instance

    # ── Helpers ────────────────────────────────────────────────────────

    async def _upsert_remote_user(self, instance_id: str, payload: dict) -> None:
        user_id = str(payload.get("user_id") or "")
        username = str(payload.get("username") or payload.get("remote_username") or "")
        if not user_id or not username:
            return
        picture_hash = payload.get("picture_hash")

        # If the peer shipped fresh picture bytes, revalidate and store
        # locally. We trust the signature on the envelope (§24.11) but
        # still re-run the image through ImageProcessor so a malicious
        # peer can't plant arbitrary bytes in the blob table.
        bytes_b64 = payload.get("picture_webp_base64")
        if bytes_b64 and self._profile_picture_repo is not None:
            try:
                raw = base64.b64decode(bytes_b64)
                webp = await ImageProcessor().generate_thumbnail(
                    raw,
                    size=PROFILE_PICTURE_MAX_DIMENSION,
                )
                local_hash = compute_picture_hash(webp)
                await self._profile_picture_repo.set_user_picture(
                    user_id,
                    bytes_webp=webp,
                    hash=local_hash,
                    width=PROFILE_PICTURE_MAX_DIMENSION,
                    height=PROFILE_PICTURE_MAX_DIMENSION,
                )
                picture_hash = local_hash
            except Exception as exc:  # pragma: no cover — defensive
                log.warning(
                    "USER_UPDATED: rejected remote picture for %s: %s",
                    user_id,
                    exc,
                )

        remote = RemoteUser(
            user_id=user_id,
            instance_id=instance_id,
            remote_username=username,
            display_name=str(payload.get("display_name") or username),
            picture_hash=picture_hash,
            bio=payload.get("bio"),
            public_key=payload.get("public_key"),
            synced_at=_now_iso(),
        )
        await self._user_repo.upsert_remote(remote)

    def _post_from_payload(self, payload: dict) -> Post | None:
        post_id = str(payload.get("id") or payload.get("post_id") or "")
        author = str(payload.get("author") or "")
        if not post_id or not author:
            return None
        type_str = str(payload.get("type") or "text")
        try:
            post_type = PostType(type_str)
        except ValueError:
            post_type = PostType.TEXT
        # Location is carried inside the encrypted payload alongside the
        # rest of the post body. Drop it silently if the peer sent
        # malformed coords — the post itself is still readable.
        location: LocationData | None = None
        raw_loc = payload.get("location")
        if isinstance(raw_loc, dict):
            try:
                location = LocationData(
                    lat=float(raw_loc["lat"]),
                    lon=float(raw_loc["lon"]),
                    label=raw_loc.get("label"),
                )
            except KeyError, TypeError, ValueError:
                location = None
        return Post(
            id=post_id,
            author=author,
            type=post_type,
            content=payload.get("content"),
            media_url=payload.get("media_url"),
            file_meta=None,
            location=location,
            created_at=parse_iso8601_lenient(payload.get("occurred_at")),
        )
