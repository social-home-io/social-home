"""Immutability guard for ``users.identity_anchor``.

``user_id`` derives from the per-user ``identity_anchor`` (a uuid4 for new
standalone users, the frozen username for existing/haos rows). The anchor is
set ONCE at provision and must NEVER change afterwards — a mutated anchor would
silently re-key the user's federated identity. This test pins that invariant:
a profile edit (display_name / bio) leaves both ``identity_anchor`` and the
derived ``user_id`` untouched.
"""

from __future__ import annotations

import pytest

from socialhome.crypto import (
    derive_instance_id,
    generate_identity_keypair,
)
from socialhome.db.database import AsyncDatabase
from socialhome.infrastructure.event_bus import EventBus
from socialhome.infrastructure.key_manager import KeyManager
from socialhome.repositories.user_repo import SqliteUserRepo
from socialhome.services.user_service import UserService


@pytest.fixture
async def user_svc(tmp_dir):
    kp = generate_identity_keypair()
    iid = derive_instance_id(kp.public_key)
    db = AsyncDatabase(tmp_dir / "test.db", batch_timeout_ms=10)
    await db.startup()
    await db.enqueue(
        """INSERT INTO instance_identity(instance_id, identity_private_key,
           identity_public_key, routing_secret) VALUES(?,?,?,?)""",
        (iid, kp.private_key.hex(), kp.public_key.hex(), "aa" * 32),
    )
    svc = UserService(
        SqliteUserRepo(db),
        EventBus(),
        own_instance_public_key=kp.public_key,
        key_manager=KeyManager.from_data_dir(tmp_dir),
    )
    yield svc
    await db.shutdown()


async def test_identity_anchor_frozen_across_profile_edit(user_svc):
    """Editing display_name/bio never mutates identity_anchor or user_id."""
    created = await user_svc.provision(
        username="pascal",
        display_name="Pascal",
    )
    anchor_before = created.identity_anchor
    user_id_before = created.user_id
    assert anchor_before is not None
    assert anchor_before != "pascal"  # uuid anchor, not the human name

    edited = await user_svc.patch_profile(
        "pascal",
        display_name="Pascal V",
        bio="hello world",
    )

    # The visible fields changed …
    assert edited.display_name == "Pascal V"
    assert edited.bio == "hello world"
    # … but the identity anchor and derived user_id are frozen.
    assert edited.identity_anchor == anchor_before
    assert edited.user_id == user_id_before

    # And the persisted row agrees (not just the returned dataclass).
    refreshed = await user_svc.get("pascal")
    assert refreshed.identity_anchor == anchor_before
    assert refreshed.user_id == user_id_before
