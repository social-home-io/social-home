"""Inbound coverage for the delegated-admin signing-seed share (v_22).

``SPACE_ADMIN_KEY_SHARE`` carries the space's Ed25519 signing seed from
the owner household to a remote admin household. The handler is
SECURITY-SENSITIVE — it persists a private signing key — so it must fail
closed: the seed is stored ONLY when the share came from the authentic
owner instance AND the local copy of the space has
``delegated_admin_authority`` enabled, with a valid 32-byte seed under a
recognised suite. Every other shape drops the event and stores nothing.
"""

from __future__ import annotations

import base64
from types import SimpleNamespace
from unittest.mock import AsyncMock

from socialhome.domain.space import (
    JoinMode,
    Space,
    SpaceFeatures,
    SpaceType,
)
from socialhome.federation.private_invite_handler import PrivateSpaceInviteHandler
from socialhome.repositories.space_remote_member_repo import (
    SqliteSpaceRemoteMemberRepo,
)
from socialhome.repositories.space_repo import SqliteSpaceRepo


OWNER = "owner-instance"
OTHER = "evil-instance"
SPACE_ID = "sp-1"
GOOD_SEED = bytes(range(32))


def _event(payload: dict, *, from_instance: str = OWNER):
    return SimpleNamespace(
        event_type="SPACE_ADMIN_KEY_SHARE",
        payload=payload,
        from_instance=from_instance,
        space_id=SPACE_ID,
    )


def _share_payload(*, seed: bytes = GOOD_SEED, suite: str = "ed25519-seed"):
    return {
        "space_id": SPACE_ID,
        "space_seed": base64.urlsafe_b64encode(seed).decode("ascii"),
        "seed_suite": suite,
    }


async def _make_handler(tmp_dir, *, delegation: bool, owner: str = OWNER):
    """A handler backed by a real space repo (so get_space_seed round-trips)
    seeded with a local stub of the owner's space."""
    from socialhome.db.database import AsyncDatabase
    from socialhome.infrastructure.key_manager import KeyManager

    db = AsyncDatabase(tmp_dir / "aks.db", batch_timeout_ms=10)
    await db.startup()
    space_repo = SqliteSpaceRepo(db, key_manager=KeyManager(b"\x07" * 32))
    await space_repo.save(
        Space(
            id=SPACE_ID,
            name="S",
            owner_instance_id=owner,
            owner_username="anna",
            identity_public_key="00" * 32,
            config_sequence=0,
            features=SpaceFeatures(delegated_admin_authority=delegation),
            space_type=SpaceType.PRIVATE,
            join_mode=JoinMode.INVITE_ONLY,
        )
    )
    remote_members = SqliteSpaceRemoteMemberRepo(db)
    bus = AsyncMock()
    h = PrivateSpaceInviteHandler(
        bus=bus,
        space_repo=space_repo,
        remote_member_repo=remote_members,
    )
    return h, space_repo, db


async def test_accepts_seed_from_owner_with_delegation_on(tmp_dir):
    """Authentic owner + local flag ON + valid 32-byte seed + correct suite
    → the seed is stored and round-trips."""
    h, space_repo, db = await _make_handler(tmp_dir, delegation=True)
    try:
        assert await space_repo.get_space_seed(SPACE_ID) is None
        await h._on_admin_key_share(_event(_share_payload()))
        stored = await space_repo.get_space_seed(SPACE_ID)
        assert stored == GOOD_SEED
    finally:
        await db.shutdown()


async def test_rejects_seed_from_non_owner(tmp_dir):
    """SECURITY: a share from an instance that is NOT the space's owner is
    dropped — the seed MUST NOT be stored."""
    h, space_repo, db = await _make_handler(tmp_dir, delegation=True)
    try:
        await h._on_admin_key_share(_event(_share_payload(), from_instance=OTHER))
        assert await space_repo.get_space_seed(SPACE_ID) is None
    finally:
        await db.shutdown()


async def test_rejects_when_local_flag_off(tmp_dir):
    """SECURITY: even from the authentic owner, if the local copy of the
    space has delegated_admin_authority OFF, the share is dropped."""
    h, space_repo, db = await _make_handler(tmp_dir, delegation=False)
    try:
        await h._on_admin_key_share(_event(_share_payload()))
        assert await space_repo.get_space_seed(SPACE_ID) is None
    finally:
        await db.shutdown()


async def test_rejects_unknown_suite(tmp_dir):
    """An unrecognised seed_suite is dropped (crypto-suite rule — no
    default fallback)."""
    h, space_repo, db = await _make_handler(tmp_dir, delegation=True)
    try:
        await h._on_admin_key_share(_event(_share_payload(suite="rsa-2048")))
        assert await space_repo.get_space_seed(SPACE_ID) is None
    finally:
        await db.shutdown()


async def test_rejects_short_seed(tmp_dir):
    """A seed that doesn't decode to exactly 32 bytes is dropped."""
    h, space_repo, db = await _make_handler(tmp_dir, delegation=True)
    try:
        await h._on_admin_key_share(_event(_share_payload(seed=b"\x01" * 16)))
        assert await space_repo.get_space_seed(SPACE_ID) is None
    finally:
        await db.shutdown()


async def test_rejects_malformed_seed(tmp_dir):
    """A non-base64 seed string is dropped (decode wrapped in try/except)."""
    h, space_repo, db = await _make_handler(tmp_dir, delegation=True)
    try:
        await h._on_admin_key_share(
            _event(
                {
                    "space_id": SPACE_ID,
                    "space_seed": "!!!not base64!!!",
                    "seed_suite": "ed25519-seed",
                }
            )
        )
        assert await space_repo.get_space_seed(SPACE_ID) is None
    finally:
        await db.shutdown()


async def test_rejects_unknown_space(tmp_dir):
    """A share for a space_id we hold no local stub for is dropped."""
    h, space_repo, db = await _make_handler(tmp_dir, delegation=True)
    try:
        ev = _event(_share_payload())
        ev.payload["space_id"] = "no-such-space"
        await h._on_admin_key_share(ev)
        # The real space's seed stays unset.
        assert await space_repo.get_space_seed(SPACE_ID) is None
    finally:
        await db.shutdown()


async def test_handler_registered(tmp_dir):
    """attach_to wires _on_admin_key_share for SPACE_ADMIN_KEY_SHARE."""
    from socialhome.domain.federation import FederationEventType

    h, _space_repo, db = await _make_handler(tmp_dir, delegation=True)
    try:
        registered = {}

        class _Reg:
            def register(self, et, handler):
                registered[et] = handler

        fed = SimpleNamespace(_event_registry=_Reg())
        h.attach_to(fed)
        assert (
            registered[FederationEventType.SPACE_ADMIN_KEY_SHARE]
            == h._on_admin_key_share
        )
    finally:
        await db.shutdown()
