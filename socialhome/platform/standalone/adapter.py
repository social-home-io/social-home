"""Standalone platform adapter (§platform/standalone).

Authenticates requests using SHA-256-hashed bearer tokens stored in
``platform_tokens``. Users, tokens, and instance configuration are managed
entirely within the local SQLite database — no external calls are made.

Audio transcription and AI data generation raise
:class:`NotImplementedError` in v1.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
from datetime import datetime, timezone
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, AsyncIterable, Mapping

import aiohttp

from ... import app_keys as K
from ..adapter import ExternalUser, InstanceConfig, _extract_bearer

if TYPE_CHECKING:
    from aiohttp import web

    from ...config import Config
    from ...db import AsyncDatabase

log = logging.getLogger(__name__)


def _sha256(token: str) -> str:
    """Return the hex SHA-256 digest of the raw token string."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class StandaloneAdapter:
    """Platform adapter backed entirely by the local SQLite database.

    :param db: Open :class:`~socialhome.db.AsyncDatabase` instance.
    :param config: Runtime :class:`~socialhome.config.Config`.
    :param options: Raw ``[standalone]`` TOML section. Reserved for
        future per-adapter settings; unused in v1.
    """

    __slots__ = ("_db", "_config", "_options", "_session")

    def __init__(
        self,
        db: "AsyncDatabase",
        config: "Config",
        options: Mapping[str, Any] | None = None,
        *,
        session: aiohttp.ClientSession | None = None,
    ) -> None:
        self._db = db
        self._config = config
        self._options: Mapping[str, Any] = options or MappingProxyType({})
        self._session: aiohttp.ClientSession | None = session

    # ── Authentication ────────────────────────────────────────────────────

    async def authenticate(self, request: "web.Request") -> ExternalUser | None:
        """Extract a bearer token from the request and delegate to :meth:`authenticate_bearer`."""
        token = _extract_bearer(request)
        if not token:
            return None
        return await self.authenticate_bearer(token)

    async def authenticate_bearer(self, token: str) -> ExternalUser | None:
        """Validate ``token`` by SHA-256 hashing and looking it up in ``platform_tokens``.

        Joins ``platform_tokens`` with ``platform_users`` so a single query
        returns the full user record. Tokens past their ``expires_at`` (when
        set) are rejected.
        """
        token_hash = _sha256(token)
        row = await self._db.fetchone(
            """
            SELECT
                u.username,
                u.display_name,
                u.picture_url,
                u.is_admin,
                u.email,
                t.expires_at
            FROM platform_tokens t
            JOIN platform_users u ON u.username = t.username
            WHERE t.token_hash = ?
            """,
            (token_hash,),
        )
        if row is None:
            return None

        # Check expiry (stored as ISO-8601 UTC text, nullable).
        if row["expires_at"] is not None:
            try:
                expires = datetime.fromisoformat(row["expires_at"])
                if expires.tzinfo is None:
                    expires = expires.replace(tzinfo=timezone.utc)
                if datetime.now(timezone.utc) > expires:
                    return None
            except ValueError:
                # Unparseable expiry — treat as expired.
                return None

        return ExternalUser(
            username=row["username"],
            display_name=row["display_name"],
            picture_url=row["picture_url"],
            is_admin=bool(row["is_admin"]),
            email=row["email"],
        )

    # ── Password-based token issuance (§auth/token) ───────────────────────

    async def issue_bearer_token(
        self,
        username: str,
        password: str,
        *,
        label: str = "web",
    ) -> str | None:
        """Verify credentials and mint a fresh bearer token (§POST /api/auth/token).

        Returns the raw token string the client must present as
        ``Authorization: Bearer <token>`` on subsequent requests, or
        ``None`` if the credentials are invalid. Tokens are stored
        only as SHA-256 hashes — once in ``platform_tokens`` (the
        platform-layer session log) and once in ``api_tokens`` keyed on
        the matching ``users`` row so the application's
        :class:`BearerTokenStrategy` can resolve them. Without the
        ``api_tokens`` mirror, ``GET /api/me`` would 401 immediately
        after a successful login — the standalone session token would
        never reach the auth middleware.
        """
        row = await self._db.fetchone(
            "SELECT password_hash FROM platform_users WHERE username=?",
            (username,),
        )
        if row is None or not row["password_hash"]:
            return None
        stored: str = row["password_hash"]
        if not self._verify_password(password, stored):
            return None

        raw = secrets.token_urlsafe(32)
        token_id = secrets.token_urlsafe(16)
        token_hash = _sha256(raw)
        await self._db.enqueue(
            "INSERT INTO platform_tokens(token_id, username, token_hash) VALUES(?,?,?)",
            (token_id, username, token_hash),
        )
        # Mirror into ``api_tokens`` so the application-layer bearer
        # strategy (which joins users → api_tokens) accepts this token.
        # Skips silently when the matching ``users`` row is absent —
        # that's a deployment misconfiguration rather than a runtime
        # error and the platform_tokens row is still useful for audit.
        user_row = await self._db.fetchone(
            "SELECT user_id FROM users WHERE username=?",
            (username,),
        )
        if user_row is not None and user_row["user_id"]:
            await self._db.enqueue(
                """
                INSERT INTO api_tokens(token_id, user_id, label, token_hash)
                VALUES(?, ?, ?, ?)
                """,
                (token_id, user_row["user_id"], label, token_hash),
            )
        return raw

    @staticmethod
    def hash_password(password: str, *, salt: bytes | None = None) -> str:
        """Return a scrypt hash in ``scrypt$<N>$<r>$<p>$<salt_hex>$<hash_hex>`` form.

        Stdlib-only — ``hashlib.scrypt`` is available on every Python 3.14
        build we target. Parameters are chosen to be secure for a household
        server (N=2^15, r=8, p=1 — ~100ms on commodity hardware).
        """
        # N*r*128 stays ≤16 MB so we sit comfortably under OpenSSL's
        # default EVP memory limit (~32 MB). ~50 ms on commodity hardware.
        n = 2**14
        r = 8
        p = 1
        if salt is None:
            salt = os.urandom(16)
        dk = hashlib.scrypt(
            password.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=32
        )
        return f"scrypt${n}${r}${p}${salt.hex()}${dk.hex()}"

    @staticmethod
    def _verify_password(password: str, stored: str) -> bool:
        if not stored.startswith("scrypt$"):
            return False
        try:
            _, n_s, r_s, p_s, salt_hex, hash_hex = stored.split("$", 5)
            n, r, p = int(n_s), int(r_s), int(p_s)
            salt = bytes.fromhex(salt_hex)
            expected = bytes.fromhex(hash_hex)
            dk = hashlib.scrypt(
                password.encode("utf-8"),
                salt=salt,
                n=n,
                r=r,
                p=p,
                dklen=len(expected),
            )
            return hmac.compare_digest(dk, expected)
        except ValueError, KeyError:
            return False

    # ── User listing ──────────────────────────────────────────────────────

    async def list_external_users(self) -> list[ExternalUser]:
        """Return all rows from ``platform_users``."""
        rows = await self._db.fetchall("SELECT * FROM platform_users")
        return [self._row_to_user(r) for r in rows]

    async def get_external_user(self, username: str) -> ExternalUser | None:
        """Return the user with ``username`` from ``platform_users``, or ``None``."""
        row = await self._db.fetchone(
            "SELECT * FROM platform_users WHERE username = ?",
            (username,),
        )
        if row is None:
            return None
        return self._row_to_user(row)

    # ── Instance config ───────────────────────────────────────────────────

    async def get_instance_config(self) -> InstanceConfig:
        """Read location from ``instance_identity``, with fallback to config defaults."""
        row = await self._db.fetchone(
            "SELECT home_lat, home_lon, home_label FROM instance_identity WHERE id='self'",
        )

        if row and row["home_lat"] is not None and row["home_lon"] is not None:
            lat = float(row["home_lat"])
            lon = float(row["home_lon"])
            label = row["home_label"] or self._config.instance_name
        else:
            lat = 0.0
            lon = 0.0
            label = self._config.instance_name

        return InstanceConfig(
            location_name=label,
            latitude=lat,
            longitude=lon,
            time_zone="UTC",
            currency="USD",
        )

    # ── Federation inbox base URL (§11) ───────────────────────────────────

    async def get_federation_base(self) -> str | None:
        """Return ``[standalone].external_url`` + ``/federation/inbox``.

        ``[standalone].external_url`` is the publicly-reachable base the
        admin has configured — "https://social.example.com". We append
        the inbox path so the coordinator can build per-peer URLs by
        concatenating the peer's ``local_inbox_id``.

        Returns ``None`` when the option is unset; the pairing route
        converts that to a 422 ``NOT_CONFIGURED`` so the admin knows to
        set the URL before issuing a QR.
        """
        raw = self._options.get("external_url") if self._options else None
        if not raw:
            return None
        base = str(raw).rstrip("/")
        if not base:
            return None
        return f"{base}/federation/inbox"

    # ── Push notifications ────────────────────────────────────────────────

    async def send_push(
        self,
        user: ExternalUser,
        title: str,
        message: str,
        data: dict | None = None,
    ) -> None:
        """POST push payload to ``platform_users.notify_endpoint``. No-op if absent.

        Best-effort — all errors are swallowed and logged at DEBUG level.
        """
        row = await self._db.fetchone(
            "SELECT notify_endpoint FROM platform_users WHERE username = ?",
            (user.username,),
        )
        if row is None or not row["notify_endpoint"]:
            return

        endpoint: str = row["notify_endpoint"]
        payload: dict = {"title": title, "message": message}
        if data:
            payload["data"] = data

        session = self._session
        if session is None:
            log.debug(
                "standalone: send_push to %r skipped — no shared HTTP session wired",
                user.username,
            )
            return

        try:
            async with session.post(endpoint, json=payload) as resp:
                if resp.status not in (200, 201, 204):
                    log.debug(
                        "standalone: send_push to %r returned %d",
                        user.username,
                        resp.status,
                    )
        except aiohttp.ClientError as exc:
            log.debug(
                "standalone: send_push to %r failed: %s",
                user.username,
                exc,
            )

    # ── Lifecycle hooks ────────────────────────────────────────────────────

    async def on_startup(self, app: "web.Application") -> None:
        """Standalone-mode startup wiring.

        Two responsibilities:

        * Pick up the shared aiohttp session for :meth:`send_push`.
        * **First-boot admin provisioning.** When ``platform_users`` is
          empty (a fresh data dir, no pairing has happened yet), seed
          a single ``admin`` user so the SPA login form actually has
          something to authenticate against. The username, password
          source (``$SH_ADMIN_PASSWORD`` or generated), and the printed-
          once password are documented in :meth:`_bootstrap_admin`.
        """
        if self._session is None:
            self._session = app[K.http_session_key]
        await self._bootstrap_admin()

    async def _bootstrap_admin(self) -> None:
        """Seed the first admin user when ``platform_users`` is empty.

        Idempotent: any pre-existing row short-circuits the bootstrap.
        Honors two environment overrides:

        * ``SH_ADMIN_USERNAME`` — defaults to ``"admin"``.
        * ``SH_ADMIN_PASSWORD`` — when unset, a random urlsafe password
          is generated and printed (once) to the log so the operator
          can capture it. Never persisted in plaintext anywhere.

        The freshly-created user is wired across both the platform side
        (``platform_users`` for password verification) and the domain
        side (``users`` for the bearer-auth join) with matching
        ``username``. Without the ``users`` row, downstream
        ``user_repo.get_user_by_token_hash`` would never find the
        principal once a token gets minted.
        """
        existing = await self._db.fetchone(
            "SELECT 1 FROM platform_users LIMIT 1",
        )
        if existing is not None:
            return

        username = os.environ.get("SH_ADMIN_USERNAME", "admin").strip() or "admin"
        env_pw = os.environ.get("SH_ADMIN_PASSWORD")
        password = env_pw if env_pw else secrets.token_urlsafe(16)
        display_name = "Admin"

        # Defensive: tests / external migrations may have already seeded a
        # ``users`` row for this username before the standalone adapter
        # boots (the conftest in tests/routes does this). Skip the bootstrap
        # entirely so we don't fight a UNIQUE conflict on ``users.username``.
        existing_user = await self._db.fetchone(
            "SELECT 1 FROM users WHERE username=?",
            (username,),
        )
        if existing_user is not None:
            return

        pw_hash = self.hash_password(password)
        await self._db.enqueue(
            """
            INSERT INTO platform_users(username, display_name, is_admin, password_hash)
            VALUES(?, ?, 1, ?)
            ON CONFLICT(username) DO NOTHING
            """,
            (username, display_name, pw_hash),
        )

        # Mirror into ``users`` so bearer auth can resolve the principal.
        # We mint a stable user_id so re-running the bootstrap with a
        # cleared platform_users table doesn't leave dangling rows.
        user_id = f"uid-{username}"
        await self._db.enqueue(
            """
            INSERT INTO users(username, user_id, display_name, is_admin)
            VALUES(?, ?, ?, 1)
            ON CONFLICT(username) DO UPDATE SET is_admin=1
            """,
            (username, user_id, display_name),
        )

        # Log once. With ``SH_ADMIN_PASSWORD`` set we avoid printing the
        # secret (the operator already knows it); with a generated one
        # we MUST print or the user can never log in.
        if env_pw:
            log.warning(
                "standalone: bootstrapped admin user %r (password from "
                "SH_ADMIN_PASSWORD)",
                username,
            )
        else:
            log.warning(
                "standalone: bootstrapped admin user %r with generated "
                "password %r — change it via the SPA / API; rerun with "
                "SH_ADMIN_PASSWORD to set a known value before first boot",
                username,
                password,
            )

    async def on_cleanup(self, app: "web.Application") -> None:  # noqa: RUF029
        """No-op — the shared session is owned by :mod:`socialhome.app`."""

    def get_extra_services(self) -> dict:
        """Standalone provides no extra services."""
        return {}

    def get_extra_routes(self) -> list[tuple[str, type]]:
        """Standalone provides no extra routes."""
        return []

    @property
    def supports_bearer_token_auth(self) -> bool:
        """Standalone supports bearer-token authentication."""
        return True

    async def fire_event(self, event_type: str, data: dict) -> bool:
        """No-op — standalone has no external event bus."""
        return False

    # ── Not implemented in v1 ─────────────────────────────────────────────

    @property
    def supports_stt(self) -> bool:
        """Standalone has no first-party STT backend in v1."""
        return False

    async def transcribe_audio(
        self,
        audio_bytes: bytes,
        language: str = "en",
    ) -> str:
        raise NotImplementedError(
            "StandaloneAdapter does not support audio transcription in v1"
        )

    async def stream_transcribe_audio(
        self,
        audio_stream: AsyncIterable[bytes],
        *,
        language: str = "en",
        sample_rate: int = 16000,
        channels: int = 1,
    ) -> str:
        raise NotImplementedError(
            "StandaloneAdapter does not support audio transcription in v1"
        )

    async def generate_ai_data(
        self,
        *,
        task_name: str,
        instructions: str,
    ) -> str:
        raise NotImplementedError(
            "StandaloneAdapter does not support AI data generation in v1"
        )

    # ── Location override ─────────────────────────────────────────────────

    async def update_location(
        self,
        latitude: float,
        longitude: float,
        location_name: str,
    ) -> InstanceConfig:
        """Persist a location override to ``instance_identity`` and return updated config."""
        lat = round(float(latitude), 4)
        lon = round(float(longitude), 4)

        await self._db.enqueue(
            """
            UPDATE instance_identity
               SET home_lat = ?, home_lon = ?, home_label = ?
             WHERE id = 'self'
            """,
            (lat, lon, location_name),
        )

        return InstanceConfig(
            location_name=location_name,
            latitude=lat,
            longitude=lon,
            time_zone="UTC",
            currency="USD",
        )

    # ── Internals ─────────────────────────────────────────────────────────

    @staticmethod
    def _row_to_user(row) -> ExternalUser:
        """Convert a ``platform_users`` row to an :class:`ExternalUser`."""
        return ExternalUser(
            username=row["username"],
            display_name=row["display_name"],
            picture_url=row["picture_url"],
            is_admin=bool(row["is_admin"]),
            email=row["email"],
        )
