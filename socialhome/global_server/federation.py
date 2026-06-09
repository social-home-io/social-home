"""GFS federation service — instance registration, event relay, subscriptions.

Business logic only — all SQL lives in :mod:`.repositories`. Crypto
helpers are reused from :mod:`socialhome.crypto` (no duplication).

Fan-out delivery is **WebSocket-primary, HTTPS-fallback** (spec §24.12):
if a paired SH instance has an open ``/gfs/ws`` WebSocket, the event is
pushed over that connection; otherwise it falls back to an HTTPS POST
to the subscriber's inbox URL.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import aiohttp

from ..crypto import b64url_decode, verify_ed25519
from .domain import ClientInstance, GfsSubscriber, GlobalSpace
from .repositories import AbstractGfsFederationRepo

if TYPE_CHECKING:
    from .ws_registry import GfsWebSocketRegistry

log = logging.getLogger(__name__)

#: Storage cap for a published space's ``about_markdown``. Generous for a
#: real "about" blurb; bounds DB growth + the public-page render cost from
#: an oversized publish. The public renderer applies its own (smaller) cap.
MAX_ABOUT_MARKDOWN_CHARS: int = 8000

#: Max display_name length accepted by ``update_instance`` (chars). Mirrors
#: the household-name bound on the HFS side.
MAX_DISPLAY_NAME_CHARS: int = 80

#: Freshness window for the signed instance-update timestamp (seconds). A
#: ``ts`` further than this from now is treated as a replay and rejected —
#: same ±300 s tolerance the §24.11 inbound pipeline uses.
INSTANCE_UPDATE_TS_SKEW_SECONDS: int = 300


class GfsFederationService:
    """Lightweight federation relay for the GFS process.

    Responsible for:
    * Registering/updating client household instances.
    * Verifying Ed25519 signatures on inbound publish requests.
    * Fanning out events to all subscribers (WS push, HTTPS fallback).
    * Managing space subscription lists.
    * Listing all known global spaces.
    """

    __slots__ = ("_repo", "_ws_registry")

    def __init__(
        self,
        repo: AbstractGfsFederationRepo,
        ws_registry: "GfsWebSocketRegistry | None" = None,
    ) -> None:
        self._repo = repo
        self._ws_registry = ws_registry

    async def register_instance(
        self,
        instance_id: str,
        public_key: str,
        inbox_url: str,
        *,
        display_name: str = "",
        auto_accept: bool = False,
    ) -> None:
        """Register or update a client household instance."""
        await self._repo.upsert_instance(
            ClientInstance(
                instance_id=instance_id,
                display_name=display_name,
                public_key=public_key,
                inbox_url=inbox_url,
                status="active" if auto_accept else "pending",
                auto_accept=auto_accept,
            )
        )
        log.debug("GFS: registered instance %s inbox=%s", instance_id, inbox_url)

    async def publish_event(
        self,
        space_id: str,
        event_type: str,
        payload: object,
        from_instance: str,
        signature: str = "",
        *,
        session: aiohttp.ClientSession | None = None,
    ) -> list[str]:
        """Relay an event to all subscribers of *space_id*.

        Validates the Ed25519 *signature* using the public key registered
        for *from_instance*. Returns the list of instance_ids successfully
        notified.

        Raises :class:`PermissionError` when *from_instance* is unknown,
        the signature is missing / malformed / invalid, the space was never
        published, or *from_instance* is not the space's owning instance.
        The signature is MANDATORY: an empty signature is rejected (a
        registered peer must not be able to publish another household's
        space content unsigned).

        Only the space's **owning instance** may relay events for it. The
        GFS has no space-membership roster (it knows registered instances,
        space ``owning_instance``, and subscribers — not who joined), so the
        owning instance is the only relationship it can authoritatively
        check. A non-owner peer that catches a space_id (they travel in
        discovery links) must not be able to inject events into another
        household's space fan-out, even with a signature valid for its own
        key. A multi-publisher model would be a deliberate feature (the GFS
        learning membership), not an implicit allow-any-registered-instance.
        """
        inst = await self._repo.get_instance(from_instance)
        if inst is None:
            raise PermissionError(f"Unknown instance: {from_instance}")

        if not signature:
            raise PermissionError("Invalid Ed25519 signature")
        canonical = json.dumps(
            {
                "space_id": space_id,
                "event_type": event_type,
                "payload": payload,
                "from_instance": from_instance,
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        raw_key = bytes.fromhex(inst.public_key)
        try:
            raw_sig = b64url_decode(signature)
        except (ValueError, TypeError) as exc:
            raise PermissionError("Invalid Ed25519 signature") from exc
        if not verify_ed25519(raw_key, canonical, raw_sig):
            raise PermissionError("Invalid Ed25519 signature")

        # The space must already be published (no auto-mint of an ownership
        # row from an event — mirrors subscribe), and only its owning
        # instance may relay events for it.
        existing = await self._repo.get_space(space_id)
        if existing is None:
            raise PermissionError("space not published")
        if existing.owning_instance != from_instance:
            raise PermissionError("not the owner of this space")

        subscribers = await self._repo.list_subscribers(
            space_id,
            exclude=from_instance,
        )

        event_body = {
            "space_id": space_id,
            "event_type": event_type,
            "payload": payload,
            "from_instance": from_instance,
        }

        return await self._fan_out(subscribers, event_body, session)

    async def subscribe(
        self,
        instance_id: str,
        space_id: str,
        ts: str,
        signature: str,
    ) -> None:
        """Add *instance_id* as a subscriber of *space_id*.

        Authenticated the same way as :meth:`update_instance`: the request
        is signed by *instance_id* over the canonical ``{instance_id,
        space_id, ts}`` JSON and verified against the instance's registered
        public key. Because the signature binds to the *instance_id* in the
        body, a caller can only subscribe **itself** — it can't sign as
        another household. The signed ``ts`` is replay-guarded (±300 s).

        Rejects (``PermissionError``) an unknown instance, a missing /
        malformed / invalid signature, a stale timestamp, and a space the
        GFS has never seen published (no auto-creation of a pending row from
        an unauthenticated demand signal — a subscription must target a real
        published space).
        """
        inst = await self._repo.get_instance(instance_id)
        if inst is None:
            raise PermissionError(f"Unknown instance: {instance_id}")

        if not signature:
            raise PermissionError("Invalid Ed25519 signature")
        canonical = json.dumps(
            {
                "instance_id": instance_id,
                "space_id": space_id,
                "ts": ts,
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        raw_key = bytes.fromhex(inst.public_key)
        try:
            raw_sig = b64url_decode(signature)
        except (ValueError, TypeError) as exc:
            raise PermissionError("Invalid Ed25519 signature") from exc
        if not verify_ed25519(raw_key, canonical, raw_sig):
            raise PermissionError("Invalid Ed25519 signature")

        # Replay guard: same ±300 s tolerance / tz-aware ISO 8601 rule as
        # update_instance.
        try:
            parsed = datetime.fromisoformat(ts)
        except (ValueError, TypeError) as exc:
            raise PermissionError("Stale timestamp") from exc
        if parsed.tzinfo is None:
            raise PermissionError("Stale timestamp")
        now = datetime.now(timezone.utc)
        if abs((now - parsed).total_seconds()) > INSTANCE_UPDATE_TS_SKEW_SECONDS:
            raise PermissionError("Stale timestamp")

        # A subscription must target a space the GFS already knows about.
        # Auto-minting a row from an (un)authenticated subscribe let any
        # caller seed arbitrary space ids — reject unknown ids instead.
        existing = await self._repo.get_space(space_id)
        if existing is None:
            raise PermissionError("space not published")

        await self._repo.add_subscriber(
            space_id=space_id,
            instance_id=instance_id,
        )
        log.debug("GFS: %s subscribed to space %s", instance_id, space_id)

    async def unsubscribe(self, instance_id: str, space_id: str) -> None:
        """Remove *instance_id* from subscribers of *space_id*."""
        await self._repo.remove_subscriber(
            space_id=space_id,
            instance_id=instance_id,
        )
        log.debug("GFS: %s unsubscribed from space %s", instance_id, space_id)

    async def list_spaces(
        self,
        *,
        status: str | None = None,
    ) -> list[GlobalSpace]:
        """Return global/public spaces known to this GFS node.

        The public ``GET /gfs/spaces`` endpoint passes ``status='active'``
        to hide pending + banned rows. Internal callers (admin, tests)
        can pass ``status=None`` to see everything.
        """
        return await self._repo.list_spaces(status=status)

    async def get_space(self, space_id: str) -> GlobalSpace | None:
        """Single-space lookup — used by ``GET /gfs/spaces/{id}`` so SH
        clients fetching the metadata for a discovery-link can mirror
        the space row locally before subscribing."""
        return await self._repo.get_space(space_id)

    async def hide_space(self, space_id: str) -> None:
        """Mark a space as ``banned`` so it drops off ``GET /gfs/spaces``.

        Used by ``DELETE /gfs/spaces/{id}/unpublish`` when the owning
        HFS retracts the listing. We keep the row so the GFS admin's
        audit trail survives; a later ``publish_space`` call from the
        owner will flip the status back.
        """
        existing = await self._repo.get_space(space_id)
        if existing is None:
            return
        await self._repo.upsert_space(
            GlobalSpace(
                space_id=existing.space_id,
                owning_instance=existing.owning_instance,
                name=existing.name,
                description=existing.description,
                about_markdown=existing.about_markdown,
                cover_url=existing.cover_url,
                min_age=existing.min_age,
                target_audience=existing.target_audience,
                accent_color=existing.accent_color,
                status="banned",
                subscriber_count=existing.subscriber_count,
                posts_per_week=existing.posts_per_week,
                published_at=existing.published_at,
            )
        )

    async def publish_space(
        self,
        *,
        space_id: str,
        owning_instance: str,
        name: str,
        description: str | None = None,
        about_markdown: str | None = None,
        cover_url: str | None = None,
        icon_url: str | None = None,
        min_age: int = 0,
        target_audience: str = "all",
        accent_color: str = "#D2542A",
        primary_color: str = "#D2542A",
        signature: str = "",
    ) -> GlobalSpace:
        """Register / refresh a space row from the owning instance.

        Drives the ``POST /gfs/spaces/{id}/publish`` route. The publish
        body is signed by the owning HFS so a malicious peer can't
        flip another household's space metadata; signature is verified
        against the registered ``ClientInstance.public_key``.
        Auto-accepted clients land as ``status='active'`` (visible on
        ``GET /gfs/spaces``); pending clients stay pending until the
        GFS admin flips them.
        """
        inst = await self._repo.get_instance(owning_instance)
        if inst is None:
            raise PermissionError(
                f"Unknown owning_instance: {owning_instance}",
            )
        # Signature is MANDATORY (same trust model as update_instance): an
        # empty / malformed / invalid signature is a hard PermissionError, so
        # a registered peer can't overwrite another household's space listing.
        if not signature:
            raise PermissionError("Invalid Ed25519 signature")
        canonical = json.dumps(
            {
                "space_id": space_id,
                "owning_instance": owning_instance,
                "name": name,
                "description": description or "",
                "about_markdown": about_markdown or "",
                "cover_url": cover_url or "",
                "icon_url": icon_url or "",
                "min_age": min_age,
                "target_audience": target_audience,
                "accent_color": accent_color,
                "primary_color": primary_color,
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        raw_key = bytes.fromhex(inst.public_key)
        try:
            raw_sig = b64url_decode(signature)
        except (ValueError, TypeError) as exc:
            raise PermissionError("Invalid Ed25519 signature") from exc
        if not verify_ed25519(raw_key, canonical, raw_sig):
            raise PermissionError("Invalid Ed25519 signature")
        # Bound the stored ``about_markdown`` (verified above against the
        # full value, so the signature still holds). The public page caps
        # rendering too; capping at storage avoids DB bloat from a paired
        # instance publishing an oversized blob. Truncate rather than
        # reject so a slightly-long About still publishes.
        if about_markdown and len(about_markdown) > MAX_ABOUT_MARKDOWN_CHARS:
            about_markdown = about_markdown[:MAX_ABOUT_MARKDOWN_CHARS]
        existing = await self._repo.get_space(space_id)
        # Owner is immutable after first publish. space_id is a public,
        # owner-chosen UUID that travels in discovery links, so a registered
        # peer that learns it could otherwise re-publish the row with
        # owning_instance=itself — validly signed by its OWN key — and seize
        # the listing. Reject any publish whose owner differs from the stored
        # one (first-publisher wins; only that instance can refresh it).
        if existing is not None and existing.owning_instance != owning_instance:
            raise PermissionError("space already owned by another instance")
        # Preserve subscriber_count / posts_per_week / published_at from
        # the existing row — those are GFS-side bookkeeping, not the
        # owner's to declare. Only the owner's name / description /
        # cover travel with the publish.
        next_status = "active" if inst.auto_accept else "pending"
        if existing is not None and existing.status == "banned":
            next_status = "banned"
        space = GlobalSpace(
            space_id=space_id,
            owning_instance=owning_instance,
            name=name,
            description=description,
            about_markdown=about_markdown,
            cover_url=cover_url,
            icon_url=icon_url,
            min_age=min_age,
            target_audience=target_audience,
            accent_color=accent_color,
            primary_color=primary_color,
            status=next_status,
            subscriber_count=existing.subscriber_count if existing else 0,
            posts_per_week=existing.posts_per_week if existing else 0.0,
            published_at=existing.published_at if existing else "",
        )
        await self._repo.upsert_space(space)
        log.info(
            "GFS: published space %s (owner=%s, status=%s)",
            space_id,
            owning_instance,
            next_status,
        )
        return space

    async def update_instance(
        self,
        instance_id: str,
        display_name: str,
        ts: str,
        signature: str,
    ) -> None:
        """Update a registered instance's display_name. Signed by the
        instance and verified against its registered public key (same
        trust model as publish_space — a peer can't rename another
        household). Rejects unknown instances, bad signatures, and stale
        timestamps (replay guard)."""
        inst = await self._repo.get_instance(instance_id)
        if inst is None:
            raise PermissionError(f"Unknown instance: {instance_id}")

        # Signature is REQUIRED here (unlike publish_space's optional-sig
        # branch): an empty or invalid signature is a hard PermissionError.
        if not signature:
            raise PermissionError("Invalid Ed25519 signature")
        canonical = json.dumps(
            {
                "instance_id": instance_id,
                "display_name": display_name,
                "ts": ts,
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        raw_key = bytes.fromhex(inst.public_key)
        try:
            raw_sig = b64url_decode(signature)
        except (ValueError, TypeError) as exc:
            raise PermissionError("Invalid Ed25519 signature") from exc
        if not verify_ed25519(raw_key, canonical, raw_sig):
            raise PermissionError("Invalid Ed25519 signature")

        # Replay guard: the signed ``ts`` must be a fresh, tz-aware ISO 8601
        # timestamp within ±300 s of now. Unparseable / naive timestamps are
        # rejected too (a missing offset is treated as untrusted).
        try:
            parsed = datetime.fromisoformat(ts)
        except (ValueError, TypeError) as exc:
            raise PermissionError("Stale timestamp") from exc
        if parsed.tzinfo is None:
            raise PermissionError("Stale timestamp")
        now = datetime.now(timezone.utc)
        if abs((now - parsed).total_seconds()) > INSTANCE_UPDATE_TS_SKEW_SECONDS:
            raise PermissionError("Stale timestamp")

        cleaned = display_name.strip()
        if not cleaned or len(cleaned) > MAX_DISPLAY_NAME_CHARS:
            raise ValueError("display_name must be 1-80 chars")

        await self._repo.set_instance_display_name(instance_id, cleaned)
        log.info("GFS: instance %s renamed to %r", instance_id, cleaned)

    # ── Fan-out ──────────────────────────────────────────────────────────

    async def _fan_out(
        self,
        subscribers: list[GfsSubscriber],
        event_body: dict,
        session: aiohttp.ClientSession | None,
    ) -> list[str]:
        """Deliver *event_body* to each subscriber.

        Tries the SH↔GFS WebSocket first (push frame ``{type:"relay", ...}``).
        If no socket is registered for the subscriber or the send fails,
        falls back to an HTTPS POST to the subscriber's inbox URL.
        """
        own_session = session is None
        active: aiohttp.ClientSession = (
            session if session is not None else aiohttp.ClientSession()
        )
        push_frame = {"type": "relay", **event_body}
        try:
            delivered: list[str] = []
            for sub in subscribers:
                # WebSocket push first.
                if self._ws_registry is not None and await self._ws_registry.send(
                    sub.instance_id,
                    push_frame,
                ):
                    delivered.append(sub.instance_id)
                    continue

                # HTTPS-inbox fallback.
                try:
                    async with active.post(
                        sub.inbox_url,
                        json=event_body,
                        timeout=aiohttp.ClientTimeout(total=10),
                    ) as resp:
                        if resp.status < 400:
                            delivered.append(sub.instance_id)
                        else:
                            log.warning(
                                "GFS fan-out: %s returned HTTP %s",
                                sub.inbox_url,
                                resp.status,
                            )
                except Exception as exc:
                    log.warning(
                        "GFS fan-out: failed to deliver to %s: %s",
                        sub.inbox_url,
                        exc,
                    )
            return delivered
        finally:
            if own_session:
                await active.close()
