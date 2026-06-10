"""Author-side producer for the public space-content relay (Phase 5a2).

Bus subscriber on :class:`SpacePostCreated`. When a PUBLIC/GLOBAL space
post is created locally on a household that *holds the space's signing
seed* (the owner, or a delegated admin per the owner-offline epic), this
service relays the post to the space's GFS subscribers via the
content-blind GFS relay:

1. Build the inner content payload (post id, author identity, content,
   media refs, created_at) — including ``author_pk`` so the subscriber
   can self-certify the author, and ``post_id`` for dedupe.
2. Encrypt it under the per-space AES-256 content key (the *existing*
   epoch key — no new key) so the GFS and any relay see only ciphertext.
3. Wrap as ``{space_id, epoch, encrypted_payload}`` and authority-sign
   with the space seed (:func:`sign_authority_event`) under
   ``space_post_public`` so the GFS can authorize the relay against the
   TOFU-pinned space public key without ever learning the content.
4. POST to every GFS the space is published to via
   :meth:`GfsConnectionService.publish_space_event`.

Mirrors :class:`socialhome.services.moment_public_outbound.MomentPublicOutbound`.

Each relayed post carries TWO signatures: the outer **space-authority**
signature (this seed-holder, verified against the pinned space key — proves
the relay is authorised) AND an inner **per-author** signature
(``author_sig``, the author's household identity seed signing the inner
content via :func:`author_signing_bytes`, verified against ``author_pk`` —
proves the named author wrote it). Without the per-author signature,
``author_pk`` / ``author_user_id`` are both public, so a seed-holder could
attribute a post to any member; the signature closes that hole.

Scope (Phase 5a2): only a *seed-holding* household relays, and only its OWN
household's locally-authored posts (the producer's ``author`` must be a local
user — enforced below — so this household always holds the author's identity
seed and can produce ``author_sig``). A plain member's public post reaches
subscribers only when a seed-holder (owner or delegated admin) relays —
accepted for this phase. Deletes/edits are a follow-up (the GFS relay is
created-only today).

Follow-up (out of scope): relaying a *remote* member's post — where this
seed-holder does NOT hold the author's identity seed — needs the author's
``author_sig`` propagated from the author's household through the mesh to the
relaying seed-holder, then carried unchanged into the inner payload. Until
that exists, a remote member's public post is not relayed (the local-author
guard below skips it).

This service is the encryption boundary: the cleartext post never leaves
in a GFS-bound envelope (CLAUDE.md Encryption-First Rule). If the space
has no content key, :meth:`SpaceContentEncryption.encrypt` raises
``RuntimeError`` rather than degrading to plaintext.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from ..authority_sig import (
    AUTHORITY_EVENT_SPACE_POST_PUBLIC,
    sign_authority_event,
    strip_authority_sig_fields,
)
from ..crypto import b64url_encode, sign_ed25519
from ..domain.events import SpacePostCreated
from ..domain.space import SpaceType
from ..infrastructure.event_bus import EventBus
from .space_public_author import author_signing_bytes

if TYPE_CHECKING:
    from ..repositories.space_repo import AbstractSpaceRepo
    from ..repositories.user_repo import AbstractUserRepo
    from .gfs_connection_service import GfsConnectionService
    from .space_crypto_service import SpaceContentEncryption

log = logging.getLogger(__name__)

#: Only these tiers relay to the GFS public-relay path. PRIVATE / HOUSEHOLD
#: spaces are never publicly discoverable, so a post in one must not leave
#: the member households.
_PUBLIC_TIERS: frozenset[SpaceType] = frozenset({SpaceType.PUBLIC, SpaceType.GLOBAL})


class SpacePublicOutbound:
    """Bus-event → GFS-relay producer for public/global space posts."""

    __slots__ = (
        "_bus",
        "_spaces",
        "_crypto",
        "_users",
        "_gfs",
        "_own_instance_id",
        "_own_instance_pk",
        "_own_identity_seed",
    )

    def __init__(
        self,
        *,
        bus: EventBus,
        space_repo: "AbstractSpaceRepo",
        space_crypto: "SpaceContentEncryption",
        user_repo: "AbstractUserRepo",
        gfs_service: "GfsConnectionService",
    ) -> None:
        self._bus = bus
        self._spaces = space_repo
        self._crypto = space_crypto
        self._users = user_repo
        self._gfs = gfs_service
        self._own_instance_id: str = ""
        #: This household's 32-byte Ed25519 identity public key. Shipped as
        #: ``author_pk`` inside the encrypted payload so the subscriber can
        #: self-certify ``derive_user_id(author_pk, username) ==
        #: author_user_id`` (mirrors the moments self-cert).
        self._own_instance_pk: bytes = b""
        #: This household's 32-byte Ed25519 identity *seed*. Signs the inner
        #: ``author_sig`` over the content (verified against ``author_pk`` by
        #: the subscriber). The producer only relays its own household's
        #: locally-authored posts, so this seed can always author-sign them.
        self._own_identity_seed: bytes = b""

    def attach_identity(
        self,
        *,
        own_instance_id: str,
        own_instance_public_key: bytes,
        own_identity_seed: bytes,
    ) -> None:
        self._own_instance_id = own_instance_id
        self._own_instance_pk = own_instance_public_key
        self._own_identity_seed = own_identity_seed

    def wire(self) -> None:
        self._bus.subscribe(SpacePostCreated, self._on_space_post_created)

    async def _on_space_post_created(self, event: SpacePostCreated) -> None:
        # Loop guard: an inbound-driven publish (re-emitted by the
        # federation inbound after receiving a post) carries
        # ``origin_instance_id`` — never fan it back out.
        if event.origin_instance_id is not None:
            return
        if not event.space_id:
            return
        post = event.post
        # Calendar-derived posts are minted locally on every household by
        # the CalendarFeedBridge from the federated calendar event — a
        # relay would double up. Mirrors SpacePostOutbound.
        if post.linked_event_id is not None:
            return
        space = await self._spaces.get(event.space_id)
        if space is None or space.space_type not in _PUBLIC_TIERS:
            return
        # Only a seed-holder (owner or delegated admin) relays. A NULL seed
        # means this household can't authority-sign — skip silently.
        seed = await self._spaces.get_space_seed(event.space_id)
        if seed is None:
            return
        if (
            not self._own_instance_id
            or not self._own_instance_pk
            or not self._own_identity_seed
        ):
            log.warning(
                "space_public.outbound: identity not attached — "
                "dropping relay for space %s",
                event.space_id,
            )
            return
        author = await self._users.get_by_user_id(post.author)
        if author is None:
            # Author isn't a LOCAL user (system/bot/remote member). We don't
            # hold their identity seed, so we cannot produce the per-author
            # ``author_sig`` — skip rather than relay an unattributable post.
            # Relaying remote-authored posts is a follow-up (see module
            # docstring): it needs the author_sig propagated through the mesh.
            return

        inner: dict[str, object] = {
            "post_id": post.id,
            "space_id": event.space_id,
            "author_user_id": post.author,
            "author_pk": self._own_instance_pk.hex(),
            "author_username": author.username,
            "type": post.type.value,
            "content": post.content,
            "media_url": post.media_url,
            "image_urls": list(post.image_urls),
            "created_at": post.created_at.isoformat() if post.created_at else None,
            "hidden_from_feed": post.hidden_from_feed,
            "origin_instance_id": self._own_instance_id,
        }
        if post.location is not None:
            inner["location"] = {
                "lat": post.location.lat,
                "lon": post.location.lon,
                "label": post.location.label,
            }
        # Per-author signature: the author's household identity seed signs the
        # canonical, domain-separated bytes over the attributable inner fields
        # (excluding ``author_sig`` itself). Verified by the subscriber against
        # ``author_pk`` so a relaying seed-holder can't forge authorship.
        inner["author_sig"] = b64url_encode(
            sign_ed25519(self._own_identity_seed, author_signing_bytes(inner))
        )
        # Encrypt under the existing per-space epoch key. Raises if no key —
        # we never relay plaintext (Encryption-First Rule).
        try:
            epoch, ct = await self._crypto.encrypt(
                event.space_id, json.dumps(inner).encode("utf-8")
            )
        except RuntimeError:
            log.warning(
                "space_public.outbound: no content key for space %s — "
                "cannot relay post %s",
                event.space_id,
                post.id,
            )
            return
        envelope: dict = {
            "space_id": event.space_id,
            "epoch": epoch,
            "encrypted_payload": ct,
        }
        sig = sign_authority_event(
            event_type=AUTHORITY_EVENT_SPACE_POST_PUBLIC,
            space_id=event.space_id,
            payload=strip_authority_sig_fields(envelope),
            space_seed=seed,
        )
        envelope.update(sig)
        try:
            await self._gfs.publish_space_event(
                space_id=event.space_id,
                event_type=AUTHORITY_EVENT_SPACE_POST_PUBLIC,
                payload=envelope,
                from_instance=self._own_instance_id,
            )
        except Exception:
            log.exception(
                "space_public.outbound: relay failed for space=%s post=%s",
                event.space_id,
                post.id,
            )
