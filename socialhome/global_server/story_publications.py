"""GFS story-publication registry (§stories_public).

The GFS-side counterpart to :class:`StoryPublicationService`. Its job
is narrow: when a SH instance posts a signed publish envelope, this
service records the row + mints share tokens. When the public landing
page resolves an ``(instance_id, story_id, token)`` URL triple, it
returns enough metadata for the page to bootstrap a WebRTC offer.

The registry **never** receives or stores story content. It holds only:
- one row in ``gfs_story_publications`` per ``(story_id, instance_id)``
  with the absolute ``expires_at`` and a cached publish signature.
- N rows in ``gfs_story_tokens`` per publication — each a revocable
  share link with an optional human label.

Author-online detection reads :class:`GfsWebSocketRegistry`: a
publication is only servable while the author's instance has a live
WS to this GFS, since that is the same WS we use to push the WebRTC
signalling offer to the author when a viewer arrives.
"""

from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass
from time import time

from .domain import GfsStoryPublication, GfsStoryToken
from .repositories import (
    AbstractGfsStoryPublicationRepo,
    AbstractGfsStoryTokenRepo,
)
from .ws_registry import GfsWebSocketRegistry

log = logging.getLogger(__name__)


@dataclass(slots=True, frozen=True)
class ResolvedToken:
    """Result of a public landing-page token lookup."""

    token: GfsStoryToken
    publication: GfsStoryPublication


class StoryPublicationRegistry:
    """Records publish rows + mints / revokes share tokens."""

    __slots__ = ("_pubs", "_tokens", "_ws", "_base_url")

    def __init__(
        self,
        pubs: AbstractGfsStoryPublicationRepo,
        tokens: AbstractGfsStoryTokenRepo,
        ws: GfsWebSocketRegistry,
        *,
        base_url: str,
    ) -> None:
        self._pubs = pubs
        self._tokens = tokens
        self._ws = ws
        # Trim a trailing slash so URL building stays a single template.
        self._base_url = base_url.rstrip("/")

    # ── Publish flow ─────────────────────────────────────────────────────

    async def record_publish(
        self,
        *,
        story_id: str,
        instance_id: str,
        expires_at: int,
        publish_signature: str,
        label: str | None = None,
    ) -> tuple[GfsStoryToken, str]:
        """Upsert the publication row, mint a fresh token, return the URL.

        The first publish creates the publication; later calls keep the
        same row but mint additional tokens. Returns ``(token_row, url)``.
        """
        pub = GfsStoryPublication(
            story_id=story_id,
            instance_id=instance_id,
            expires_at=expires_at,
            published_at=int(time()),
            publish_signature=publish_signature,
        )
        await self._pubs.upsert(pub)
        return await self.mint_token(
            story_id=story_id,
            instance_id=instance_id,
            label=label,
        )

    async def mint_token(
        self,
        *,
        story_id: str,
        instance_id: str,
        label: str | None = None,
    ) -> tuple[GfsStoryToken, str]:
        """Mint another share token under an existing publication."""
        existing = await self._pubs.get(story_id, instance_id)
        if existing is None:
            raise LookupError(
                f"Publication {story_id=}/{instance_id=} not found — "
                "publish first before minting extra tokens.",
            )
        token = GfsStoryToken(
            token=_new_token(),
            story_id=story_id,
            instance_id=instance_id,
            label=label,
            created_at=int(time()),
            revoked_at=None,
        )
        await self._tokens.insert(token)
        return token, self._build_url(instance_id, story_id, token.token)

    # ── Revoke / unpublish flow ──────────────────────────────────────────

    async def revoke_token(self, token: str, instance_id: str) -> bool:
        """Mark a single share token revoked. Returns True if anything
        actually changed (so the route can return 404 on stale tokens)."""
        n = await self._tokens.revoke(token, instance_id, now=int(time()))
        return n > 0

    async def remove_publish(self, story_id: str, instance_id: str) -> bool:
        """Drop the publication row. CASCADE drops every token under it.
        Returns True if a row was actually deleted."""
        n = await self._pubs.delete(story_id, instance_id)
        return n > 0

    # ── Public landing-page resolution ───────────────────────────────────

    async def resolve_token(self, token: str) -> ResolvedToken | None:
        """Land an opaque token on its (publication, token row) pair.

        Returns ``None`` when the token is unknown, has been revoked, or
        the publication's ``expires_at`` has passed. The ``GET /story/...``
        route turns ``None`` into 410 Gone.
        """
        hit = await self._tokens.lookup_active(token, now=int(time()))
        if hit is None:
            return None
        tok, pub = hit
        return ResolvedToken(token=tok, publication=pub)

    async def author_online(self, instance_id: str) -> bool:
        """True iff the author's SH has a live WS to this GFS.

        We only consider a publication servable when the author is
        online, since the public landing page asks GFS to push a WebRTC
        signalling offer to that exact WS. If the author is offline,
        the page returns 503 instead of trying to broker a dead offer.
        """
        return self._ws.is_connected(instance_id)

    # ── URL helper ───────────────────────────────────────────────────────

    def _build_url(self, instance_id: str, story_id: str, token: str) -> str:
        return f"{self._base_url}/story/{instance_id}/{story_id}/{token}"


def _new_token() -> str:
    """32-byte urlsafe-base64 token. Roughly the same entropy a peer
    pairing token carries."""
    return secrets.token_urlsafe(32)
