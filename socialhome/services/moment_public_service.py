"""Public-Momentum service (§Momentum-public).

Author-facing CRUD on registrations and follows. Issues signed HTTP
requests to the GFS for register / unregister / follow / unfollow;
registers a local row so the inbox can render "via GFS" chips and so
the inbound WS handler can verify per-moment signatures locally.

The fan-out itself rides the persistent WS — see
:mod:`moment_public_outbound` (author-side push) and
:mod:`moment_public_inbound` (recipient-side verify + persist).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

import aiohttp

from ..crypto import b64url_encode, sign_ed25519
from ..domain.moment_public import MomentPublicFollow, MomentPublicRegistration
from ..repositories.gfs_connection_repo import AbstractGfsConnectionRepo
from ..repositories.moment_public_repo import (
    AbstractMomentPublicFollowRepo,
    AbstractMomentPublicRegistrationRepo,
)
from ..repositories.user_repo import AbstractUserRepo

log = logging.getLogger(__name__)


class MomentPublicError(Exception):
    """Raised on any GFS round-trip failure. Mapped to HTTP 502."""

    __slots__ = ()


class MomentPublicService:
    """Author/follower bookkeeping + GFS round-trips."""

    __slots__ = (
        "_regs",
        "_follows",
        "_users",
        "_gfs",
        "_http_client",
        "_signing_key",
        "_own_instance_id",
    )

    def __init__(
        self,
        registration_repo: AbstractMomentPublicRegistrationRepo,
        follow_repo: AbstractMomentPublicFollowRepo,
        user_repo: AbstractUserRepo,
        gfs_repo: AbstractGfsConnectionRepo,
        *,
        http_client: aiohttp.ClientSession | None = None,
    ) -> None:
        self._regs = registration_repo
        self._follows = follow_repo
        self._users = user_repo
        self._gfs = gfs_repo
        self._http_client = http_client
        self._signing_key: bytes | None = None
        self._own_instance_id: str = ""

    def attach_session(self, session: aiohttp.ClientSession) -> None:
        if self._http_client is None:
            self._http_client = session

    def attach_identity(self, *, own_instance_id: str, signing_key: bytes) -> None:
        self._own_instance_id = own_instance_id
        self._signing_key = signing_key

    # ── Registrations ───────────────────────────────────────────────────

    async def register(
        self,
        *,
        user_id: str,
        gfs_id: str,
        default_share: bool = True,
    ) -> MomentPublicRegistration:
        user = await self._users.get_by_user_id(user_id)
        if user is None:
            raise LookupError(f"user {user_id!r} not found")
        conn = await self._require_active_gfs(gfs_id)
        body = {
            "user_id": user_id,
            "instance_id": self._require_instance_id(),
            "username": user.username,
            "display_name": user.display_name,
            "picture_url": _picture_url(user),
            "home_instance_pk": _hex_signing_pubkey(self._require_signing_key()),
        }
        signed = self._sign_body(body)
        try:
            async with self._client().post(
                f"{conn.inbox_url}/gfs/users/register",
                json=signed,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status >= 300:
                    raise MomentPublicError(
                        f"GFS rejected register: HTTP {resp.status}"
                    )
        except aiohttp.ClientError as exc:
            raise MomentPublicError(f"GFS register failed: {exc}") from exc
        return await self._regs.upsert(
            user_id=user_id, gfs_id=gfs_id, default_share=default_share
        )

    async def deregister(self, *, user_id: str, gfs_id: str) -> None:
        conn = await self._require_active_gfs(gfs_id)
        body = {"user_id": user_id, "instance_id": self._require_instance_id()}
        signed = self._sign_body(body)
        try:
            async with self._client().post(
                f"{conn.inbox_url}/gfs/users/{user_id}/deregister",
                json=signed,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status >= 300 and resp.status != 404:
                    raise MomentPublicError(
                        f"GFS rejected deregister: HTTP {resp.status}"
                    )
        except aiohttp.ClientError as exc:
            log.warning("moment_public deregister: %s", exc)
        finally:
            await self._regs.delete(user_id=user_id, gfs_id=gfs_id)

    async def list_registrations(self, user_id: str) -> list[MomentPublicRegistration]:
        return await self._regs.list_for_user(user_id)

    async def set_default_share(
        self, *, user_id: str, gfs_id: str, default_share: bool
    ) -> None:
        await self._regs.set_default_share(
            user_id=user_id, gfs_id=gfs_id, default_share=default_share
        )

    async def is_registered(self, *, user_id: str, gfs_id: str) -> bool:
        got = await self._regs.get(user_id=user_id, gfs_id=gfs_id)
        return got is not None

    async def default_share(self, *, user_id: str, gfs_id: str) -> bool:
        got = await self._regs.get(user_id=user_id, gfs_id=gfs_id)
        return bool(got and got.default_share)

    # ── Follows ─────────────────────────────────────────────────────────

    async def follow(
        self,
        *,
        follower_user_id: str,
        gfs_id: str,
        followed_user_id: str,
    ) -> MomentPublicFollow:
        conn = await self._require_active_gfs(gfs_id)
        body = {
            "follower_user_id": follower_user_id,
            "follower_instance_id": self._require_instance_id(),
            "followed_user_id": followed_user_id,
        }
        signed = self._sign_body(body)
        try:
            async with self._client().post(
                f"{conn.inbox_url}/gfs/users/{followed_user_id}/follow",
                json=signed,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                payload = await resp.json()
                if resp.status >= 300:
                    raise MomentPublicError(
                        f"GFS rejected follow: HTTP {resp.status} {payload}"
                    )
        except aiohttp.ClientError as exc:
            raise MomentPublicError(f"GFS follow failed: {exc}") from exc

        directory = payload.get("user") or {}
        return await self._follows.upsert(
            follower_user_id=follower_user_id,
            followed_user_id=followed_user_id,
            gfs_id=gfs_id,
            followed_instance_pk=str(directory.get("home_instance_pk", "")),
            followed_username=str(directory.get("username", "")),
            followed_display_name=str(directory.get("display_name", followed_user_id)),
        )

    async def unfollow(
        self,
        *,
        follower_user_id: str,
        gfs_id: str,
        followed_user_id: str,
    ) -> None:
        conn = await self._require_active_gfs(gfs_id)
        body = {
            "follower_user_id": follower_user_id,
            "follower_instance_id": self._require_instance_id(),
        }
        signed = self._sign_body(body)
        try:
            async with self._client().post(
                f"{conn.inbox_url}/gfs/users/{followed_user_id}/unfollow",
                json=signed,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status >= 300 and resp.status != 404:
                    raise MomentPublicError(
                        f"GFS rejected unfollow: HTTP {resp.status}"
                    )
        except aiohttp.ClientError as exc:
            log.warning("moment_public unfollow: %s", exc)
        finally:
            await self._follows.delete(
                follower_user_id=follower_user_id,
                followed_user_id=followed_user_id,
                gfs_id=gfs_id,
            )

    async def list_follows(self, follower_user_id: str) -> list[MomentPublicFollow]:
        return await self._follows.list_for_follower(follower_user_id)

    async def fetch_directory(self, gfs_id: str) -> list[dict]:
        """Fetch the GFS public-user directory (signed GET)."""
        conn = await self._require_active_gfs(gfs_id)
        try:
            async with self._client().get(
                f"{conn.inbox_url}/gfs/users",
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status >= 300:
                    raise MomentPublicError(
                        f"GFS directory fetch failed: HTTP {resp.status}"
                    )
                payload = await resp.json()
        except aiohttp.ClientError as exc:
            raise MomentPublicError(f"GFS directory request failed: {exc}") from exc
        users = payload.get("users")
        if not isinstance(users, list):
            return []
        return users

    # ── Internal ────────────────────────────────────────────────────────

    async def _require_active_gfs(self, gfs_id: str):
        conn = await self._gfs.get(gfs_id)
        if conn is None or conn.status != "active":
            raise MomentPublicError(
                f"GFS connection {gfs_id!r} not paired or inactive."
            )
        return conn

    def _require_instance_id(self) -> str:
        if not self._own_instance_id:
            raise MomentPublicError("MomentPublicService used before attach_identity")
        return self._own_instance_id

    def _require_signing_key(self) -> bytes:
        if self._signing_key is None:
            raise MomentPublicError("MomentPublicService used before attach_identity")
        return self._signing_key

    def _client(self) -> aiohttp.ClientSession:
        if self._http_client is None:
            raise MomentPublicError("MomentPublicService used before attach_session")
        return self._http_client

    def _sign_body(self, body: dict) -> dict:
        signing_key = self._require_signing_key()
        canonical = json.dumps(body, separators=(",", ":"), sort_keys=True).encode(
            "utf-8"
        )
        signed = dict(body)
        signed["signature"] = b64url_encode(sign_ed25519(signing_key, canonical))
        return signed


def _picture_url(user) -> str | None:
    """Best-effort canonical picture URL for the directory listing."""
    pic_hash = getattr(user, "picture_hash", None)
    user_id = getattr(user, "user_id", None)
    if pic_hash and user_id:
        return f"/api/users/{user_id}/picture?v={pic_hash}"
    return None


def _hex_signing_pubkey(signing_key: bytes) -> str:
    """Derive the Ed25519 public key (hex) from the seed.

    The federation identity already exposes ``own_public_key`` on the
    encoder; we keep this helper local so the service doesn't have to
    pull a second dependency just to advertise the verifier key in the
    register payload.
    """
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey,
    )
    from cryptography.hazmat.primitives.serialization import (
        Encoding,
        PublicFormat,
    )

    sk = Ed25519PrivateKey.from_private_bytes(signing_key[:32])
    pub_bytes = sk.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return pub_bytes.hex()


# pyright: reportUnusedImport=false
_ = datetime, timezone  # kept for forward-compat helpers
