"""GFS domain types — row-shaped dataclasses for the Global Federation Server.

Aligned with spec §24.6. Adds fraud-report, appeal, admin-session, and
cluster-node dataclasses for the full admin portal.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True, frozen=True)
class ClientInstance:
    """A registered household instance."""

    instance_id: str
    display_name: str
    public_key: str  # Ed25519 verify key (hex)
    inbox_url: str
    status: str = "pending"  # 'pending' | 'active' | 'banned'
    auto_accept: bool = False
    connected_at: str = ""  # ISO 8601


@dataclass(slots=True, frozen=True)
class GlobalSpace:
    """A global (discoverable) space published to this GFS."""

    space_id: str
    owning_instance: str
    name: str = ""
    description: str | None = None
    about_markdown: str | None = None
    cover_url: str | None = None
    min_age: int = 0
    target_audience: str = "all"
    accent_color: str = "#6366f1"
    status: str = "pending"  # 'pending' | 'active' | 'banned'
    subscriber_count: int = 0
    posts_per_week: float = 0.0
    published_at: str = ""  # ISO 8601


@dataclass(slots=True, frozen=True)
class GfsSubscriber:
    """A subscriber row: instance + inbox URL for fan-out delivery."""

    instance_id: str
    inbox_url: str


@dataclass(slots=True, frozen=True)
class GfsFraudReport:
    """A household-admin fraud report against a space or instance."""

    id: str
    target_type: str  # 'space' | 'instance'
    target_id: str
    category: str
    notes: str | None
    reporter_instance_id: str
    reporter_user_id: str | None
    status: str  # 'pending' | 'dismissed' | 'acted'
    created_at: int  # unix epoch
    reviewed_by: str | None = None
    reviewed_at: int | None = None


@dataclass(slots=True, frozen=True)
class GfsAppeal:
    """A banned household's one-shot appeal message."""

    id: str
    target_type: str  # 'space' | 'instance'
    target_id: str
    message: str
    status: str  # 'pending' | 'lifted' | 'dismissed'
    created_at: int
    decided_at: int | None = None
    decided_by: str | None = None


@dataclass(slots=True, frozen=True)
class AdminSession:
    token: str
    expires_at: int
    created_at: int


@dataclass(slots=True, frozen=True)
class ClusterNode:
    """A GFS cluster node (spec §24.10.3)."""

    node_id: str
    url: str
    public_key: str = ""
    status: str = "unknown"  # 'online' | 'offline' | 'syncing' | 'unknown'
    last_seen: str | None = None
    added_at: str = ""
    active_sync_sessions: int = 0

    @property
    def address(self) -> str:
        """Back-compat alias — older callers use ``address`` for ``url``."""
        return self.url


@dataclass(slots=True, frozen=True)
class RtcConnection:
    """Transport mode per client instance (spec §24.12).

    ``transport`` is ``'webrtc'`` when the household's DataChannel is up,
    ``'inbox'`` when falling back to HTTPS push. ``last_ping_at`` is
    bumped by each RTC-ping or HTTPS inbox fallback write so the admin UI
    can show online/offline per peer.
    """

    instance_id: str
    transport: str = "https"
    connected_at: str = ""
    last_ping_at: str = ""


@dataclass(slots=True, frozen=True)
class GfsHighlightPublication:
    """A SH instance's opt-in to share a single highlight via this GFS.

    GFS holds zero highlight bytes — only the routing metadata + a cached
    Ed25519 signature so the publish can be re-verified during audits
    or signalling. The highlight itself streams over WebRTC author →
    viewer once the public landing page bootstraps a peer connection.

    ``expires_at`` is unix epoch and mirrors the author's
    ``highlights.expires_at`` so a publication can never outlive the
    highlight it advertises.
    """

    highlight_id: str
    instance_id: str
    expires_at: int
    published_at: int
    publish_signature: str
    #: Filename of the OG-card thumbnail under
    #: :attr:`GfsConfig.og_thumbnail_dir`. ``None`` until the author
    #: uploads one. Anonymous OG crawlers fetch the image directly
    #: from the GFS — no token required, since social previews need
    #: to render in clients (Twitter, iMessage, Slack) that don't
    #: pass query strings.
    og_thumbnail_filename: str | None = None


@dataclass(slots=True, frozen=True)
class GfsHighlightToken:
    """One revocable share link under a :class:`GfsHighlightPublication`.

    Authors mint multiple tokens per publication — one per platform
    or recipient — so they can revoke a single audience without
    pulling the rest. ``revoked_at`` is ``None`` while active; setting
    it to a unix epoch makes the resolver return ``None`` immediately
    on the next public landing-page hit.
    """

    token: str
    highlight_id: str
    instance_id: str
    label: str | None
    created_at: int
    revoked_at: int | None


@dataclass(slots=True, frozen=True)
class GfsUserRegistration:
    """A user opted in to public-Momentum via this GFS (§Momentum-public).

    Lives only on the GFS. Carries the routing fields the broker
    needs (``instance_id``, ``home_instance_pk``) plus the
    user-supplied directory metadata (``display_name``,
    ``picture_url``) shown by the public ``/users`` listing.
    """

    user_id: str
    instance_id: str
    username: str
    display_name: str
    picture_url: str | None
    home_instance_pk: str
    registered_at: int
    status: str = "active"  # 'active' | 'suspended'
    bio: str | None = None
    picture_digest: str | None = None


@dataclass(slots=True, frozen=True)
class GfsUserPicture:
    """Avatar bytes mirrored onto the GFS for the public directory.

    Households running public Momentum may sit behind home-network
    NAT, so a public browser can't always hit the home instance's
    ``/api/users/{id}/picture`` endpoint. Mirroring the bytes onto
    the GFS makes the directory standalone-renderable.
    """

    user_id: str
    bytes_: bytes
    mime: str
    digest: str
    updated_at: int


@dataclass(slots=True, frozen=True)
class GfsMomentFollow:
    """A follower instance has subscribed to a registered author.

    Used by the broker's fan-out: when the author's instance pushes
    a ``moment_public`` frame, the broker reads
    ``followers_of(author_user_id)`` and forwards an
    ``incoming_public_moment`` frame over each unique
    ``follower_instance_id``'s persistent WS.
    """

    follower_user_id: str
    follower_instance_id: str
    followed_user_id: str
    created_at: int


# Backwards-compatible aliases for the pre-spec stub names so existing
# tests / imports keep working through the transition.
GfsInstance = ClientInstance
GfsSpace = GlobalSpace
