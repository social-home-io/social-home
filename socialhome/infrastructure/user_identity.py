"""Per-user identity key minting + lazy backfill (Phase 1).

Mirrors ``ensure_instance_identity``: mint a KEK-wrapped Ed25519 keypair for
every local user lacking one. The optional ML-DSA-65 PQ half is minted in-place
when the configured sig_suite needs it (deferred to the PQ-suite rollout). Only
public halves ever federate. Behaviour-neutral — legacy ``user_id`` untouched.
"""

from __future__ import annotations

import logging

from ..crypto import generate_identity_keypair
from ..db import AsyncDatabase
from ..infrastructure.key_manager import KeyManager

log = logging.getLogger(__name__)


async def ensure_user_identities(
    db: AsyncDatabase,
    key_manager: KeyManager,
    *,
    sig_suite: str,
) -> int:
    """Mint a KEK-wrapped Ed25519 identity keypair for each local user lacking one.

    Returns the number of users minted. The classical half is always minted;
    the PQ (ML-DSA-65) half is deferred to the PQ-suite rollout — when
    ``sig_suite`` names it we only log, never mint.
    """
    needs_pq = "mldsa65" in sig_suite
    rows = await db.fetchall(
        "SELECT username FROM users "
        "WHERE deleted_at IS NULL AND user_identity_public_key IS NULL",
    )
    minted = 0
    for row in rows:
        kp = generate_identity_keypair()
        await db.enqueue(
            "UPDATE users SET user_identity_public_key=?, "
            "user_identity_private_key=? WHERE username=?",
            (
                kp.public_key.hex(),
                key_manager.encrypt(kp.private_key),
                row["username"],
            ),
        )
        minted += 1
    if needs_pq:
        log.info("user-identity: PQ-half minting deferred to PQ suite rollout")
    return minted
