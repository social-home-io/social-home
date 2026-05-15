"""Direct message / group DM domain types (§5.2 / §23.47).

:class:`Conversation` models a 1:1 DM or a group DM. Messages are
:class:`ConversationMessage` records with a small type vocabulary.
:class:`MessageReaction` records per-user reactions on a message.

All types are immutable dataclasses. Mutations return new instances.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum


class ConversationType(StrEnum):
    DM = "dm"  # exactly 2 participants
    GROUP_DM = "group_dm"  # 3+ participants; may carry an optional name


# Allowed ``type`` values for a :class:`ConversationMessage`.
#
# Media attachments (``image`` / ``video`` / ``file``) carry their
# bytes via ``media_url`` plus the ``file_name`` / ``mime_type`` /
# ``file_size_bytes`` siblings below. Same-household renders straight
# from the local-signed URL; cross-household uses the preview-now-
# sync-later flow (a tiny preview is embedded in the encrypted
# ``DM_MESSAGE`` envelope, the full bytes follow on a
# ``DM_MEDIA_BLOB`` event). ``transcript`` and ``location`` carry
# their data inside ``content``.
MESSAGE_TYPES: frozenset[str] = frozenset(
    {
        "text",
        "image",
        "video",
        "file",
        "transcript",
        "location",
    }
)


@dataclass(slots=True, frozen=True)
class Conversation:
    id: str
    type: ConversationType
    created_at: datetime

    name: str | None = None  # set for group DMs, None for 1:1
    last_message_at: datetime | None = None
    bot_enabled: bool = False  # True → HA bot-bridge may post to this DM


@dataclass(slots=True, frozen=True)
class ConversationMessage:
    id: str
    conversation_id: str
    sender_user_id: str
    content: str
    created_at: datetime

    type: str = "text"
    media_url: str | None = None
    #: Original filename for ``type='file'`` (or a user-friendly label
    #: for image/video uploads). NULL for ``text`` / ``transcript`` /
    #: ``location`` and for any media without a label.
    file_name: str | None = None
    #: IANA media type — drives the receiver's render branch
    #: (``image/*`` → inline ``<img>``, ``video/*`` → ``<video>``,
    #: anything else → file pill with a glyph + filename + size).
    mime_type: str | None = None
    #: Authoritative byte count of the full-quality media (post-
    #: transcoding for image / video, raw for ``file``). Surfaces in
    #: the bubble as "1.2 MB" so the recipient knows what they're
    #: about to load on a metered connection.
    file_size_bytes: int | None = None
    #: Stable identifier shared with the follow-up ``DM_MEDIA_BLOB``
    #: event when this message rides cross-household. NULL for local
    #: messages or non-media types.
    media_blob_id: str | None = None
    #: Cross-household sync state. NULL when the message is local OR
    #: the full bytes have arrived (``media_url`` now points at the
    #: local-stored full media). ``'pending'`` = the bubble renders
    #: the preview embedded in the envelope while waiting for the
    #: blob; ``'failed'`` = sender gave up after retry-budget
    #: exhaustion.
    media_sync_status: str | None = None
    reply_to_id: str | None = None
    #: Highlight-frame reply (§Highlights). Set when the user replied to a
    #: highlight frame from the viewer; the snapshot below freezes a
    #: thumbnail + caption so the reply stays meaningful after the
    #: source frame is removed by the retention scheduler.
    reply_to_highlight_frame_id: str | None = None
    reply_to_highlight_frame_snapshot: str | None = None  # JSON
    deleted: bool = False
    edited_at: datetime | None = None

    def soft_delete(self) -> "ConversationMessage":
        return copy.replace(
            self,
            content="",
            media_url=None,
            file_name=None,
            mime_type=None,
            file_size_bytes=None,
            media_blob_id=None,
            media_sync_status=None,
            deleted=True,
        )

    def edit(
        self, new_content: str, *, now: datetime | None = None
    ) -> "ConversationMessage":
        return copy.replace(
            self,
            content=new_content,
            edited_at=now or datetime.now(timezone.utc),
        )


@dataclass(slots=True, frozen=True)
class MessageReaction:
    message_id: str
    user_id: str
    emoji: str
    reacted_at: datetime


@dataclass(slots=True, frozen=True)
class ConversationMember:
    """One participant row of a :class:`Conversation` (local users only)."""

    conversation_id: str
    username: str  # local username (FK to users)
    joined_at: str
    last_read_at: str | None = None
    history_visible_from: str | None = None
    # Soft-delete for 1:1 DMs — set when a participant leaves. None = active.
    deleted_at: str | None = None


@dataclass(slots=True, frozen=True)
class RemoteConversationMember:
    """One participant row for a remote user in a federated conversation."""

    conversation_id: str
    instance_id: str
    remote_username: str
    joined_at: str
    history_visible_from: str | None = None
