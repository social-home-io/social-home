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
import pathlib
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


#: Hard cap on the bytes of an uploaded OG thumbnail. Twitter/iMessage/
#: Slack render at most ~1MB; we settle for 200 KB so abuse can't fill
#: the GFS disk and a malformed JPEG can't bury the operator's storage.
MAX_OG_THUMBNAIL_BYTES: int = 200 * 1024


class StoryPublicationRegistry:
    """Records publish rows + mints / revokes share tokens."""

    __slots__ = ("_pubs", "_tokens", "_ws", "_base_url", "_og_dir")

    def __init__(
        self,
        pubs: AbstractGfsStoryPublicationRepo,
        tokens: AbstractGfsStoryTokenRepo,
        ws: GfsWebSocketRegistry,
        *,
        base_url: str,
        og_thumbnail_dir: pathlib.Path,
    ) -> None:
        self._pubs = pubs
        self._tokens = tokens
        self._ws = ws
        # Trim a trailing slash so URL building stays a single template.
        self._base_url = base_url.rstrip("/")
        self._og_dir = og_thumbnail_dir
        try:
            self._og_dir.mkdir(parents=True, exist_ok=True)
        except OSError:  # pragma: no cover — ops issue, log on first use
            pass

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
        Returns True if a row was actually deleted. Also wipes the
        cached OG thumbnail file so a re-publish doesn't accidentally
        keep showing the previous social-card image."""
        await self.remove_og_thumbnail(story_id=story_id, instance_id=instance_id)
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

    async def get_publication(
        self,
        story_id: str,
        instance_id: str,
    ) -> GfsStoryPublication | None:
        """Read-only fetch of the publication row.

        Routes that need the OG thumbnail filename or the absolute
        ``expires_at`` query through this rather than poking the
        repo directly so the registry stays the single point of
        truth for the §stories_public storage state.
        """
        return await self._pubs.get(story_id, instance_id)

    async def author_online(self, instance_id: str) -> bool:
        """True iff the author's SH has a live WS to this GFS.

        We only consider a publication servable when the author is
        online, since the public landing page asks GFS to push a WebRTC
        signalling offer to that exact WS. If the author is offline,
        the page returns 503 instead of trying to broker a dead offer.
        """
        return self._ws.is_connected(instance_id)

    # ── OG thumbnail cache ───────────────────────────────────────────────

    async def store_og_thumbnail(
        self,
        *,
        story_id: str,
        instance_id: str,
        jpeg_bytes: bytes,
    ) -> str:
        """Persist a JPEG thumbnail for the publication's OG card.

        Returns the filename so route handlers can echo it back. The
        filename is deterministic from ``(instance_id, story_id)`` so
        a re-upload overwrites in place — no orphan files. Caller is
        responsible for verifying the publication exists.
        """
        if len(jpeg_bytes) > MAX_OG_THUMBNAIL_BYTES:
            raise ValueError(
                f"OG thumbnail exceeds {MAX_OG_THUMBNAIL_BYTES} bytes",
            )
        if not _looks_like_jpeg(jpeg_bytes):
            raise ValueError("Uploaded body is not a JPEG image")
        filename = _og_filename(instance_id, story_id)
        path = self._og_dir / filename
        path.write_bytes(jpeg_bytes)
        await self._pubs.set_og_thumbnail(story_id, instance_id, filename)
        return filename

    async def remove_og_thumbnail(
        self,
        *,
        story_id: str,
        instance_id: str,
    ) -> None:
        """Drop the cached OG thumbnail. Called from
        :meth:`remove_publish` so a re-publish doesn't keep serving an
        old preview, and exposed for the standalone unpublish route."""
        existing = await self._pubs.get(story_id, instance_id)
        if existing is None or not existing.og_thumbnail_filename:
            return
        path = self._og_dir / existing.og_thumbnail_filename
        try:
            path.unlink(missing_ok=True)
        except OSError:  # pragma: no cover - defensive
            pass
        await self._pubs.set_og_thumbnail(story_id, instance_id, None)

    def og_thumbnail_path(self, filename: str) -> pathlib.Path:
        """Resolve a stored thumbnail filename back to its on-disk path.

        Defends against directory traversal: ``filename`` must be a
        bare filename (no path segments). Returns the joined path
        regardless of whether the file exists — callers stat to
        decide.
        """
        if "/" in filename or "\\" in filename or filename.startswith("."):
            raise ValueError("invalid filename")
        return self._og_dir / filename

    # ── URL helper ───────────────────────────────────────────────────────

    def _build_url(self, instance_id: str, story_id: str, token: str) -> str:
        return f"{self._base_url}/story/{instance_id}/{story_id}/{token}"

    def og_image_url(self, instance_id: str, story_id: str) -> str:
        """Return the public URL for the OG image. Stable per story so
        social-card crawlers can cache it across token rotations."""
        return f"{self._base_url}/story/{instance_id}/{story_id}/og.jpg"


def _new_token() -> str:
    """32-byte urlsafe-base64 token. Roughly the same entropy a peer
    pairing token carries."""
    return secrets.token_urlsafe(32)


def _og_filename(instance_id: str, story_id: str) -> str:
    """Deterministic ``{instance}_{story}.jpg`` so re-upload overwrites."""
    safe_inst = "".join(c if c.isalnum() else "_" for c in instance_id)
    safe_story = "".join(c if c.isalnum() else "_" for c in story_id)
    return f"{safe_inst}_{safe_story}.jpg"


def _looks_like_jpeg(data: bytes) -> bool:
    """Magic-byte check — ``FF D8 FF`` for any JPEG variant.

    Cheap defence against an attacker uploading non-image bytes that
    a browser might mis-interpret. The author SH side should already
    have run the bytes through the image processor.
    """
    return len(data) >= 3 and data[:3] == b"\xff\xd8\xff"
