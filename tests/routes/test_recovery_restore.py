"""Integration tests for ``POST /api/setup/recovery/restore``.

The restore endpoint reconstitutes a household's identity from a sealed
Recovery Kit on a fresh box. The app under the ``client`` fixture auto-mints
a throwaway identity at startup and leaves setup required; restore must wipe
that throwaway trust state, restore the kit, mark setup complete, and schedule
a process restart so the restored identity/KEK load on reboot.

``request_process_restart`` is patched so the test process is not SIGTERM'd.
"""

from __future__ import annotations

import base64
import os
from unittest.mock import patch

from socialhome.app_keys import setup_service_key
from socialhome.crypto import derive_instance_id, generate_identity_keypair
from socialhome.db.database import AsyncDatabase
from socialhome.infrastructure.key_manager import KeyManager
from socialhome.services.recovery_kit_service import RecoveryKitService

PASS = "pw pw pw pw"
RESTART_TARGET = "socialhome.routes.setup.request_process_restart"


async def _build_foreign_kit(tmp_path) -> tuple[str, str]:
    """Build a Recovery Kit from a DIFFERENT identity than the live box.

    Returns ``(kit_b64, instance_id)`` — the kit's own instance_id, which the
    restore must bring back (replacing the auto-minted one).
    """
    dir2 = tmp_path / "kitsrc"
    dir2.mkdir()
    (dir2 / ".kek_salt").write_bytes(os.urandom(KeyManager.KEK_BYTES))
    km = KeyManager.from_data_dir(dir2)
    kp = generate_identity_keypair()
    iid = derive_instance_id(kp.public_key)

    db2 = AsyncDatabase(dir2 / "k.db", batch_timeout_ms=10)
    await db2.startup()
    try:
        await db2.enqueue(
            "INSERT INTO instance_identity(instance_id, identity_private_key,"
            " identity_public_key, routing_secret) VALUES(?,?,?,?)",
            (iid, km.encrypt(os.urandom(32)), kp.public_key.hex(), "aa" * 32),
        )
        kit = await RecoveryKitService(db2, dir2).build_kit(PASS)
    finally:
        await db2.shutdown()

    return base64.b64encode(kit).decode("ascii"), iid


def _url() -> str:
    return "/api/setup/recovery/restore"


async def test_setup_required_before_restore(client):
    """Sanity: the freshly-booted box has setup required (auto-minted only)."""
    setup = client.app[setup_service_key]
    assert await setup.is_required() is True


async def test_restore_happy_path(client, tmp_dir):
    kit_b64, kit_iid = await _build_foreign_kit(tmp_dir)

    with patch(RESTART_TARGET) as restart:
        resp = await client.post(_url(), json={"kit_b64": kit_b64, "passphrase": PASS})

    assert resp.status == 200
    data = await resp.json()
    assert data == {"instance_id": kit_iid, "restart_required": True}

    # The auto-minted identity was replaced by the kit's identity.
    ident = await client._db.fetchone(
        "SELECT instance_id FROM instance_identity WHERE id='self'"
    )
    assert ident["instance_id"] == kit_iid

    # A restart was scheduled.
    restart.assert_called_once()

    # Setup is now complete.
    assert await client.app[setup_service_key].is_required() is False


async def test_restore_wrong_passphrase(client, tmp_dir):
    kit_b64, _ = await _build_foreign_kit(tmp_dir)

    # Capture the box's auto-minted identity before the failed restore.
    before = await client._db.fetchone(
        "SELECT instance_id FROM instance_identity WHERE id='self'"
    )
    assert before is not None

    with patch(RESTART_TARGET) as restart:
        resp = await client.post(
            _url(), json={"kit_b64": kit_b64, "passphrase": "not the passphrase"}
        )

    assert resp.status == 422
    data = await resp.json()
    assert data["error"]["code"] == "BAD_KIT"
    # No restart scheduled on a failed restore.
    restart.assert_not_called()
    # The kit is validated BEFORE the destructive wipe, so a wrong passphrase
    # must leave the existing identity untouched (not strand the box).
    after = await client._db.fetchone(
        "SELECT instance_id FROM instance_identity WHERE id='self'"
    )
    assert after is not None
    assert after["instance_id"] == before["instance_id"]


async def test_restore_bad_base64(client):
    with patch(RESTART_TARGET) as restart:
        resp = await client.post(
            _url(), json={"kit_b64": "!!!not base64!!!", "passphrase": PASS}
        )
    assert resp.status == 422
    assert (await resp.json())["error"]["code"] == "UNPROCESSABLE"
    restart.assert_not_called()


async def test_restore_missing_fields(client):
    with patch(RESTART_TARGET) as restart:
        resp = await client.post(_url(), json={"passphrase": PASS})
    assert resp.status == 422
    assert (await resp.json())["error"]["code"] == "UNPROCESSABLE"
    restart.assert_not_called()


async def test_restore_gated_after_setup_complete(client, tmp_dir):
    kit_b64, _ = await _build_foreign_kit(tmp_dir)
    # Mark setup complete first — the gate must reject a second restore.
    await client.app[setup_service_key].mark_complete()

    with patch(RESTART_TARGET) as restart:
        resp = await client.post(_url(), json={"kit_b64": kit_b64, "passphrase": PASS})
    assert resp.status == 409
    assert (await resp.json())["error"]["code"] == "ALREADY_COMPLETE"
    restart.assert_not_called()
