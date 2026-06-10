"""Seed-holder producer for the Phase-5b subscriber content-key handoff.

When a household subscribes to a PUBLIC/GLOBAL space at the GFS and a
seed-holder (the owner or a delegated admin) is online, the GFS notifies
the owner with a ``new_subscriber`` frame carrying the subscriber's
published identity + key-wrap material. This service answers it: it seals
the per-space content key to the subscriber's static X25519 key-wrap pubkey
and relays the sealed envelope back through the content-blind GFS as a
space-authority-signed ``space_subscriber_key_handoff`` event.

The point is to let a GFS subscriber DECRYPT the Phase-5a public-content
relay (which it receives but currently drops, having no key) — delivered
GFS-blind so the GFS (and any non-target subscriber the GFS fans it out to)
never sees the content key.

Pipeline (fail-closed at every step):

1. **Gate on seed + tier.** Act only if this household holds the space seed
   (``get_space_seed`` non-None → owner or delegated admin) and the space is
   PUBLIC/GLOBAL. A non-seed-holder can't authority-sign; a private/household
   space is never GFS-discoverable, so its key must never leave via the GFS.
2. **Verify the key-wrap binding (anti-substitution).** The key-wrap pubkey
   is learned *from the GFS*; a malicious GFS could substitute one it
   controls and read the sealed key. ``verify_keywrap_binding`` binds the
   key-wrap key to the subscriber identity end-to-end — if it fails, DROP and
   never seal. A subscriber that shipped no key-wrap key (older HFS) is
   skipped (it can't be sealed-to yet — graceful, no relay).
3. **Export the current content key** and build the same
   ``{space_content_key: {key_suite, epoch, key_base64, rotated_by}}`` meta
   that :func:`apply_space_content_key_from_metadata` consumes (mirrors the
   §D1b / rekey distribution shape).
4. **Seal** the meta to the verified key-wrap pubkey
   (:func:`seal_to_keywrap`) → ``{kem_suite, eph_pk, ciphertext}``.
5. **Wrap + authority-sign** ``{space_id, target_instance_id, sealed}`` with
   the space seed under ``space_subscriber_key_handoff`` and relay via the
   existing GFS publish path. The GFS authorizes it against the TOFU-pinned
   space pubkey (same authority-relay path as ``space_post_public``); the
   non-target subscribers it fans out to drop it (``target_instance_id`` ≠
   self, and they can't ``open_keywrap`` it anyway).

Out of scope (Phase 5b-c follow-up): the owner-offline RECONCILE, where a
seed-holder pulls the GFS subscriber list to catch up handoffs missed while
no seed-holder was online. This service is the notify-driven fast path only.
"""

from __future__ import annotations

import base64
import json
import logging
from typing import TYPE_CHECKING, Any

from ..authority_sig import (
    AUTHORITY_EVENT_SPACE_SUBSCRIBER_KEY_HANDOFF,
    sign_authority_event,
    strip_authority_sig_fields,
)
from ..domain.space import SpaceType
from ..federation.keywrap_seal import seal_to_keywrap, verify_keywrap_binding
from ..services.space_crypto_service import KEY_SUITE_AESGCM_256

if TYPE_CHECKING:
    from ..repositories.space_repo import AbstractSpaceRepo
    from .gfs_connection_service import GfsConnectionService
    from .space_crypto_service import SpaceContentEncryption

log = logging.getLogger(__name__)

#: Only these tiers ship a content key via the GFS handoff. PRIVATE /
#: HOUSEHOLD spaces are never publicly discoverable, so their key must not
#: leave the member households through a public relay.
_PUBLIC_TIERS: frozenset[SpaceType] = frozenset({SpaceType.PUBLIC, SpaceType.GLOBAL})


class SpaceSubscriberKeyOutbound:
    """``new_subscriber`` GFS frame → sealed content-key handoff producer."""

    __slots__ = ("_spaces", "_crypto", "_gfs", "_own_instance_id")

    def __init__(
        self,
        *,
        space_repo: "AbstractSpaceRepo",
        space_crypto: "SpaceContentEncryption",
        gfs_service: "GfsConnectionService",
    ) -> None:
        self._spaces = space_repo
        self._crypto = space_crypto
        self._gfs = gfs_service
        self._own_instance_id: str = ""

    def attach_identity(self, *, own_instance_id: str) -> None:
        self._own_instance_id = own_instance_id

    async def handle(self, frame: dict[str, Any]) -> None:
        """Handle one GFS ``new_subscriber`` frame. Never raises."""
        if frame.get("type") != "new_subscriber":
            return
        space_id = str(frame.get("space_id") or "")
        sub = frame.get("subscriber")
        if not space_id or not isinstance(sub, dict):
            return

        space = await self._spaces.get(space_id)
        if space is None or space.space_type not in _PUBLIC_TIERS:
            return
        # Only a seed-holder (owner or delegated admin) can authority-sign.
        seed = await self._spaces.get_space_seed(space_id)
        if seed is None:
            return
        if not self._own_instance_id:
            log.warning(
                "space_subscriber_key.outbound: identity not attached — "
                "dropping handoff for space %s",
                space_id,
            )
            return

        target_instance_id = str(sub.get("instance_id") or "")
        identity_pub_hex = str(sub.get("identity_public_key") or "")
        keywrap_pub_hex = str(sub.get("keywrap_public_key") or "")
        keywrap_sig = str(sub.get("keywrap_sig") or "")
        if not target_instance_id or not identity_pub_hex:
            return
        # A subscriber that published no key-wrap key (older HFS) can't be
        # sealed-to — skip gracefully (no relay).
        if not keywrap_pub_hex or not keywrap_sig:
            log.info(
                "space_subscriber_key.outbound: subscriber %s shipped no "
                "key-wrap key — skipping handoff for space %s",
                target_instance_id,
                space_id,
            )
            return
        try:
            identity_pub = bytes.fromhex(identity_pub_hex)
            keywrap_pub = bytes.fromhex(keywrap_pub_hex)
        except ValueError:
            log.warning(
                "space_subscriber_key.outbound: malformed subscriber keys for "
                "%s on space %s — dropped",
                target_instance_id,
                space_id,
            )
            return
        # ANTI-SUBSTITUTION GATE: verify the GFS-served key-wrap pubkey is
        # genuinely bound to the subscriber identity (derive_instance_id +
        # self-sig + 32-byte) BEFORE sealing. A swapped key (e.g. a malicious
        # GFS slipping in a key it controls) fails here → DROP, never seal.
        if not verify_keywrap_binding(
            instance_id=target_instance_id,
            identity_pub=identity_pub,
            keywrap_pub=keywrap_pub,
            keywrap_sig=keywrap_sig,
        ):
            log.warning(
                "space_subscriber_key.outbound: key-wrap binding FAILED for "
                "subscriber %s on space %s — refusing to seal (possible GFS "
                "substitution)",
                target_instance_id,
                space_id,
            )
            return

        exported = await self._crypto.export_current_key(space_id)
        if exported is None:
            log.warning(
                "space_subscriber_key.outbound: no content key for space %s — "
                "cannot hand off to %s",
                space_id,
                target_instance_id,
            )
            return
        epoch, raw_key = exported
        # Mirror the meta apply_space_content_key_from_metadata consumes (and
        # the §D1b / rekey distribution shape).
        meta = {
            "space_content_key": {
                "epoch": epoch,
                "key_suite": KEY_SUITE_AESGCM_256,
                "key_base64": base64.b64encode(raw_key).decode("ascii"),
                "rotated_by": self._own_instance_id,
            }
        }
        sealed = seal_to_keywrap(
            recipient_keywrap_pub=keywrap_pub,
            plaintext=json.dumps(meta).encode("utf-8"),
        )
        envelope: dict = {
            "space_id": space_id,
            "target_instance_id": target_instance_id,
            "sealed": sealed,
        }
        sig = sign_authority_event(
            event_type=AUTHORITY_EVENT_SPACE_SUBSCRIBER_KEY_HANDOFF,
            space_id=space_id,
            payload=strip_authority_sig_fields(envelope),
            space_seed=seed,
        )
        envelope.update(sig)
        try:
            await self._gfs.publish_space_event(
                space_id=space_id,
                event_type=AUTHORITY_EVENT_SPACE_SUBSCRIBER_KEY_HANDOFF,
                payload=envelope,
                from_instance=self._own_instance_id,
            )
        except Exception:
            log.exception(
                "space_subscriber_key.outbound: relay failed for space=%s target=%s",
                space_id,
                target_instance_id,
            )
