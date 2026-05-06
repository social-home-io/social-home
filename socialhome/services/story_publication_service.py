"""Story public-publication service (§stories_public).

Author opts a single story into public sharing via a paired GFS. The
GFS mints a one-shot share token and returns a URL the author can copy
into Twitter / email / SMS. Anyone visiting that URL gets a public
landing page; the page bootstraps a WebRTC DataChannel directly to the
author's instance, so the actual story bytes never transit GFS.

This service runs on the **author's SH** and handles only the
HTTP-signed publish / revoke / unpublish exchange with the GFS — the
WebRTC streaming layer arrives in PR2 (``StorySignalingHandler``).

Per :class:`GfsConnectionService`, every body is canonicalised
(``json.dumps(sort_keys=True, separators=(",",":"))``) and signed with
the local instance's Ed25519 key. The signature lands in a
``"signature"`` field appended to the body; the GFS-side
``_rtc_authenticate`` middleware strips and verifies it.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

import aiohttp

from ..crypto import b64url_encode, sign_ed25519
from ..repositories.gfs_connection_repo import AbstractGfsConnectionRepo
from ..repositories.story_repo import AbstractStoryRepo

log = logging.getLogger(__name__)


class StoryPublicationError(Exception):
    """Raised when a publish / revoke / unpublish round-trip fails.

    Mapped centrally by :class:`BaseView._iter` to HTTP 502 so the SPA
    surfaces a "GFS unavailable" message rather than a generic 500.
    """

    __slots__ = ()


class StoryNotFoundError(KeyError):
    """Raised when the story id is unknown OR not owned by the caller.

    Mirrors :class:`MomentNotFoundError` — same shape avoids leaking
    "exists but not yours" via 403 vs 404 split.
    """

    __slots__ = ()


class StoryPublicationService:
    """SH-side orchestrator for the publish / revoke / unpublish flow."""

    __slots__ = (
        "_stories",
        "_gfs",
        "_http_client",
        "_signing_key",
        "_own_instance_id",
    )

    def __init__(
        self,
        story_repo: AbstractStoryRepo,
        gfs_repo: AbstractGfsConnectionRepo,
        *,
        http_client: aiohttp.ClientSession | None = None,
    ) -> None:
        self._stories = story_repo
        self._gfs = gfs_repo
        self._http_client = http_client
        self._signing_key: bytes | None = None
        self._own_instance_id: str = ""

    def attach_session(self, session: aiohttp.ClientSession) -> None:
        if self._http_client is None:
            self._http_client = session

    def attach_identity(
        self,
        *,
        own_instance_id: str,
        signing_key: bytes,
    ) -> None:
        """Late-bound identity wiring — same shape as
        :meth:`ReportService.attach_gfs`. Both inputs come online during
        ``app._on_startup`` once federation identity has loaded."""
        self._own_instance_id = own_instance_id
        self._signing_key = signing_key

    # ── Public API ───────────────────────────────────────────────────────

    async def publish(
        self,
        story_id: str,
        author_user_id: str,
        *,
        gfs_id: str,
        label: str | None = None,
    ) -> dict:
        """Mint a fresh public-share token for ``story_id``.

        Returns ``{"token", "url", "label"}``. Idempotent across repeated
        calls — each call mints a *new* token (one per platform pattern),
        but the parent ``gfs_story_publications`` row is upserted, so
        the second call doesn't double-up that row.
        """
        story = await self._require_owned(story_id, author_user_id)
        if not story.expires_at:
            # Should never happen — every story carries an expires_at —
            # but bail noisily rather than POSTing a meaningless cap.
            raise StoryPublicationError("Story has no expiry — refusing publish.")

        body = {
            "story_id": story.id,
            "instance_id": self._require_instance_id(),
            "expires_at": _expires_to_unix(story.expires_at),
            "label": label,
        }
        signed = self._sign_body(body)
        conn = await self._require_active_gfs(gfs_id)
        try:
            async with self._client().post(
                f"{conn.inbox_url}/gfs/stories/{story.id}/publish",
                json=signed,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                payload = await resp.json()
                if resp.status >= 300:
                    raise StoryPublicationError(
                        f"GFS rejected publish: HTTP {resp.status} {payload}"
                    )
        except aiohttp.ClientError as exc:
            raise StoryPublicationError(f"GFS publish request failed: {exc}") from exc

        token = str(payload.get("token") or "")
        url = str(payload.get("url") or "")
        if not token or not url:
            raise StoryPublicationError("GFS publish response missing token / url.")
        # Mark the story as published *after* the GFS confirms — a network
        # failure should leave the local flag clear so the SPA doesn't
        # show "published" for a publication that never landed.
        await self._stories.mark_published(
            story.id,
            gfs_id=gfs_id,
            published_at=datetime.now(timezone.utc).isoformat(),
        )
        return {"token": token, "url": url, "label": label}

    async def revoke_token(
        self,
        story_id: str,
        author_user_id: str,
        *,
        token: str,
    ) -> None:
        """Revoke a single share token.

        Other tokens under the same publication keep working; the parent
        row stays. To pull *all* tokens, call :meth:`unpublish`.
        """
        story = await self._require_owned(story_id, author_user_id)
        gfs_id = story.public_gfs_id
        if gfs_id is None:
            raise StoryPublicationError("Story is not currently published.")
        conn = await self._require_active_gfs(gfs_id)

        body = {"token": token, "instance_id": self._require_instance_id()}
        signed = self._sign_body(body)
        try:
            async with self._client().post(
                f"{conn.inbox_url}/gfs/story_tokens/{token}/revoke",
                json=signed,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status >= 300:
                    raise StoryPublicationError(
                        f"GFS rejected revoke: HTTP {resp.status} {await resp.text()}"
                    )
        except aiohttp.ClientError as exc:
            raise StoryPublicationError(f"GFS revoke request failed: {exc}") from exc

    async def upload_og_thumbnail(
        self,
        story_id: str,
        author_user_id: str,
        *,
        jpeg_bytes: bytes,
    ) -> str:
        """Cache a JPEG thumbnail on the GFS for OG-card previews.

        The thumbnail is intentionally public — anonymous social-card
        crawlers (Twitter, Slack, iMessage) need to fetch it without
        the share token. Author opts in per-story by uploading; the
        absence of a thumbnail keeps the share link rendering as a
        generic OG card.

        Returns the public OG URL on success.
        """
        import base64

        story = await self._require_owned(story_id, author_user_id)
        gfs_id = story.public_gfs_id
        if gfs_id is None:
            raise StoryPublicationError("Story is not currently published.")
        conn = await self._require_active_gfs(gfs_id)
        body = self._sign_body(
            {
                "instance_id": self._require_instance_id(),
                "story_id": story.id,
                "image_b64": base64.b64encode(jpeg_bytes).decode("ascii"),
            }
        )
        try:
            async with self._client().post(
                f"{conn.inbox_url}/gfs/stories/{story.id}/og",
                json=body,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                payload = await resp.json()
                if resp.status >= 300:
                    raise StoryPublicationError(
                        f"GFS rejected OG upload: HTTP {resp.status} {payload}"
                    )
        except aiohttp.ClientError as exc:
            raise StoryPublicationError(
                f"GFS OG upload request failed: {exc}",
            ) from exc
        url = str(payload.get("url") or "")
        if not url:
            raise StoryPublicationError("GFS OG response missing url.")
        return url

    async def unpublish(self, story_id: str, author_user_id: str) -> None:
        """Pull the publication entirely. CASCADE on the GFS side wipes
        every token under it; the local flag clears regardless of GFS
        response so a confused author can always recover the SPA state."""
        story = await self._require_owned(story_id, author_user_id)
        gfs_id = story.public_gfs_id
        if gfs_id is None:
            return  # Idempotent — already unpublished.
        conn = await self._require_active_gfs(gfs_id)
        body = {
            "story_id": story.id,
            "instance_id": self._require_instance_id(),
        }
        signed = self._sign_body(body)
        try:
            async with self._client().post(
                f"{conn.inbox_url}/gfs/stories/{story.id}/unpublish",
                json=signed,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status >= 300 and resp.status != 404:
                    # 404 means the GFS row already vanished — fine.
                    raise StoryPublicationError(
                        f"GFS rejected unpublish: HTTP {resp.status} "
                        f"{await resp.text()}"
                    )
        except aiohttp.ClientError as exc:
            # Log but still clear the local flag — author has explicitly
            # asked to unpublish; we honour that locally even if the GFS
            # is unreachable. The retention scheduler on the GFS side
            # will eventually prune the orphan row.
            log.warning("GFS unpublish request failed: %s", exc)
        finally:
            await self._stories.mark_unpublished(story.id)

    # ── Internal helpers ─────────────────────────────────────────────────

    async def _require_owned(
        self,
        story_id: str,
        author_user_id: str,
    ):
        story = await self._stories.get_story(story_id)
        if story is None or story.author_user_id != author_user_id:
            raise StoryNotFoundError(story_id)
        return story

    async def _require_active_gfs(self, gfs_id: str):
        conn = await self._gfs.get(gfs_id)
        if conn is None or conn.status != "active":
            raise StoryPublicationError(
                f"GFS connection {gfs_id!r} not paired or inactive.",
            )
        return conn

    def _require_instance_id(self) -> str:
        if not self._own_instance_id:
            raise StoryPublicationError(
                "StoryPublicationService used before attach_identity",
            )
        return self._own_instance_id

    def _client(self) -> aiohttp.ClientSession:
        if self._http_client is None:
            raise StoryPublicationError(
                "StoryPublicationService used before attach_session",
            )
        return self._http_client

    def _sign_body(self, body: dict) -> dict:
        if self._signing_key is None:
            raise StoryPublicationError(
                "StoryPublicationService used before attach_identity",
            )
        canonical = json.dumps(body, separators=(",", ":"), sort_keys=True).encode(
            "utf-8"
        )
        signed = dict(body)
        signed["signature"] = b64url_encode(sign_ed25519(self._signing_key, canonical))
        return signed


def _expires_to_unix(iso_or_unix: str) -> int:
    """Coerce ``stories.expires_at`` (ISO-8601) into a unix epoch.

    GFS persists the cap as an integer epoch so its CHECK + index
    queries can use the same ``strftime('%s','now')`` clock the rest
    of the GFS schema does.
    """
    try:
        # Parse the ISO; Python 3.11+ accepts a trailing 'Z'.
        normalised = iso_or_unix.replace("Z", "+00:00")
        return int(datetime.fromisoformat(normalised).timestamp())
    except TypeError, ValueError:
        # Some legacy rows store unix epoch as a string.
        return int(iso_or_unix)
