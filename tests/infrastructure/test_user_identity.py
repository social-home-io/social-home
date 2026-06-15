"""Tests for per-user identity key minting + lazy backfill (Phase 1).

``ensure_user_identities`` mints a KEK-wrapped Ed25519 keypair for every local
user lacking one (mirrors ``ensure_instance_identity``). The classical half is
always minted; the PQ (ML-DSA-65) half is deferred to the PQ-suite rollout.
"""

from __future__ import annotations

import os

import pytest

from socialhome.db import AsyncDatabase
from socialhome.infrastructure.key_manager import KeyManager
from socialhome.infrastructure.user_identity import ensure_user_identities


@pytest.fixture
def key_manager(tmp_path):
    (tmp_path / ".kek_salt").write_bytes(os.urandom(KeyManager.KEK_BYTES))
    return KeyManager.from_data_dir(tmp_path)


@pytest.fixture
async def db(tmp_path):
    database = AsyncDatabase(tmp_path / "t.db", batch_timeout_ms=10)
    await database.startup()
    yield database
    await database.shutdown()


async def test_lazy_backfill_mints_classical_half(db, key_manager):
    await db.enqueue(
        "INSERT INTO users(username, user_id, display_name) "
        "VALUES('alice', 'uid-a', 'Alice')",
    )

    minted = await ensure_user_identities(db, key_manager, sig_suite="ed25519")

    assert minted == 1
    row = await db.fetchone(
        "SELECT user_identity_public_key, user_identity_private_key, "
        "       user_pq_public_key "
        "FROM users WHERE username='alice'",
    )
    assert row["user_identity_public_key"] is not None
    assert row["user_identity_private_key"] is not None
    assert row["user_pq_public_key"] is None
    seed = key_manager.decrypt(row["user_identity_private_key"])
    assert len(seed) == 32


async def test_backfill_idempotent(db, key_manager):
    await db.enqueue(
        "INSERT INTO users(username, user_id, display_name) "
        "VALUES('alice', 'uid-a', 'Alice')",
    )

    first = await ensure_user_identities(db, key_manager, sig_suite="ed25519")
    second = await ensure_user_identities(db, key_manager, sig_suite="ed25519")

    assert first == 1
    assert second == 0


async def test_skips_soft_deleted(db, key_manager):
    await db.enqueue(
        "INSERT INTO users(username, user_id, display_name, deleted_at) "
        "VALUES('ghost', 'uid-g', 'Ghost', '2026-01-01T00:00:00+00:00')",
    )

    minted = await ensure_user_identities(db, key_manager, sig_suite="ed25519")

    assert minted == 0
    row = await db.fetchone(
        "SELECT user_identity_public_key FROM users WHERE username='ghost'",
    )
    assert row["user_identity_public_key"] is None
