"""Tests for SpaceContentEncryption + space crypto helpers (§4.3)."""

from __future__ import annotations

import pytest

from socialhome.crypto import (
    derive_instance_id,
    derive_space_id,
    generate_identity_keypair,
    generate_space_keypair,
)
from socialhome.db.database import AsyncDatabase
from socialhome.domain.federation import RemoteInstance
from socialhome.infrastructure.key_manager import KeyManager
from socialhome.repositories.federation_repo import SqliteFederationRepo
from socialhome.repositories.space_key_repo import (
    SqliteSpaceKeyRepo,
)
from socialhome.services.space_crypto_service import (
    SpaceContentEncryption,
    create_space_identity,
    sign_space_config,
    verify_space_config,
)


# ─── Crypto helpers ──────────────────────────────────────────────────────


def test_derive_space_id_from_public_key():
    kp = generate_space_keypair()
    sid = derive_space_id(kp.public_key)
    assert isinstance(sid, str)
    assert len(sid) == 32
    # Same key → same id (deterministic).
    assert derive_space_id(kp.public_key) == sid


def test_derive_space_id_rejects_wrong_size():
    with pytest.raises(ValueError):
        derive_space_id(b"too-short")


def test_create_space_identity_returns_consistent_triple():
    seed, pk, sid = create_space_identity()
    assert len(seed) == 32 and len(pk) == 32
    assert sid == derive_space_id(pk)


def test_sign_and_verify_space_config_roundtrip():
    seed, pk, sid = create_space_identity()
    payload = b'{"event":"rename","new_name":"Vacation"}'
    sig = sign_space_config(payload, space_seed=seed)
    assert verify_space_config(payload, sig, space_public_key=pk) is True


def test_verify_space_config_rejects_tampering():
    seed, pk, _ = create_space_identity()
    payload = b'{"event":"rename","new_name":"Vacation"}'
    sig = sign_space_config(payload, space_seed=seed)
    # Modified payload — signature should fail.
    assert verify_space_config(b'{"event":"hijack"}', sig, space_public_key=pk) is False


def test_verify_space_config_rejects_garbage_signature():
    _, pk, _ = create_space_identity()
    assert verify_space_config(b"x", "not-base64-!!!", space_public_key=pk) is False


# ─── SpaceContentEncryption ──────────────────────────────────────────────


@pytest.fixture
async def crypto_env(tmp_dir):
    db = AsyncDatabase(tmp_dir / "t.db", batch_timeout_ms=10)
    await db.startup()
    # space_id is FK-enforced, so create a parent spaces row first.
    await db.enqueue(
        "INSERT INTO spaces(id, name, owner_instance_id, owner_username,"
        " identity_public_key) VALUES('sp-1', 'Test', 'inst-1', 'pascal', ?)",
        ("ab" * 32,),
    )
    repo = SqliteSpaceKeyRepo(db)
    kek = KeyManager.from_data_dir(tmp_dir)
    # The sealed-sender outer_signature is signed with this instance's
    # identity seed and verified against the sender's registered pubkey.
    # Register THIS instance's identity as a confirmed member of sp-1 so
    # unseal_from_gfs can resolve its own sealed sender out of the box.
    kp = generate_identity_keypair()
    seed, pub = kp.private_key, kp.public_key
    sender_iid = derive_instance_id(pub)
    fed_repo = SqliteFederationRepo(db)
    await fed_repo.save_instance(
        RemoteInstance(
            id=sender_iid,
            display_name="Sender",
            remote_identity_pk=pub.hex(),
            key_self_to_remote="x",
            key_remote_to_self="y",
            remote_inbox_url="https://peer.example/inbox",
            local_inbox_id="local-inbox-1",
        )
    )
    await db.enqueue(
        "INSERT INTO space_instances(space_id, instance_id) VALUES('sp-1', ?)",
        (sender_iid,),
    )
    crypto = SpaceContentEncryption(
        repo,
        kek,
        identity_seed=seed,
        federation_repo=fed_repo,
    )
    yield crypto, repo, sender_iid
    await db.shutdown()


async def test_export_current_key_returns_unwrapped_bytes_and_epoch(crypto_env):
    """§D1b #117 — the host hands a brand-new remote member the
    space content key inside the (already-encrypted) invite envelope
    so they can decrypt subsequent SPACE_POST_CREATED events. The
    returned bytes are the *unwrapped* AES-256 key — the federation
    layer is responsible for the secrecy of the envelope they go in."""
    crypto, _, _sender_iid = crypto_env
    await crypto.initialise_for_space("sp-1")
    result = await crypto.export_current_key("sp-1")
    assert result is not None
    epoch, raw = result
    assert epoch == 0
    assert isinstance(raw, bytes)
    assert len(raw) == 32  # AES-256


async def test_export_current_key_returns_none_when_uninitialised(crypto_env):
    """A space with no epoch key yet exports ``None`` — caller
    skips shipping the field rather than synthesising garbage."""
    crypto, _, _sender_iid = crypto_env
    # sp-1 exists as a parent row from the fixture but has no
    # epoch key until ``initialise_for_space`` runs.
    assert await crypto.export_current_key("sp-1") is None


async def test_import_key_then_decrypt_smoke(crypto_env):
    """End-to-end of the §D1b key handoff: encrypt → export the
    raw key → import (simulating receiver-side persistence) →
    decrypt under the imported key. The at-rest KEK wrap is
    re-applied on import so the row matches the local invariant."""
    crypto, _, _sender_iid = crypto_env
    await crypto.initialise_for_space("sp-1")
    epoch, ct = await crypto.encrypt("sp-1", b"hello space")
    exported = await crypto.export_current_key("sp-1")
    assert exported is not None
    _epoch, raw_key = exported
    # ``import_key`` upserts under the same (space_id, epoch), so
    # we can re-decrypt — proves the same bytes decrypt after the
    # round-trip through unwrap → re-wrap.
    await crypto.import_key("sp-1", epoch, raw_key)
    plaintext = await crypto.decrypt("sp-1", epoch, ct)
    assert plaintext == b"hello space"


async def test_import_key_rejects_wrong_length(crypto_env):
    crypto, _, _sender_iid = crypto_env
    with pytest.raises(ValueError):
        await crypto.import_key("sp-1", 0, b"too short")


async def test_apply_space_content_key_rejects_unknown_suite(crypto_env):
    """Forward-compat — receivers MUST reject suites they don't know
    rather than fall back to a default. Mirrors the kem_suite
    rejection in routed_crypto.py so the wire shape is safe to grow
    without breaking older receivers (#117)."""
    from socialhome.services.space_crypto_service import UnsupportedKeySuite
    from socialhome.services.space_service import (
        apply_space_content_key_from_metadata,
    )

    crypto, _, _sender_iid = crypto_env
    bad_meta = {
        "space_content_key": {
            "epoch": 0,
            "key_suite": "x25519+mlkem768-prophet-2030",
            "key_base64": "AAAA",
        },
    }
    with pytest.raises(UnsupportedKeySuite):
        await apply_space_content_key_from_metadata(
            "sp-1",
            meta=bad_meta,
            space_crypto_service=crypto,
        )


async def test_apply_space_content_key_accepts_default_suite_when_missing(
    crypto_env,
):
    """Older sender that doesn't include ``key_suite`` (first
    revision of this protocol) defaults to ``aesgcm-256`` — the
    only suite supported today — so the key still lands."""
    import base64

    from socialhome.services.space_service import (
        apply_space_content_key_from_metadata,
    )

    crypto, _, _sender_iid = crypto_env
    raw = b"x" * 32
    meta = {
        "space_content_key": {
            "epoch": 7,
            # No ``key_suite`` — older sender.
            "key_base64": base64.b64encode(raw).decode("ascii"),
        },
    }
    await apply_space_content_key_from_metadata(
        "sp-1",
        meta=meta,
        space_crypto_service=crypto,
    )
    exported = await crypto.export_current_key("sp-1")
    assert exported is not None
    assert exported == (7, raw)


async def test_initialise_for_space_creates_epoch_zero(crypto_env):
    crypto, _, _sender_iid = crypto_env
    epoch = await crypto.initialise_for_space("sp-1")
    assert epoch == 0


async def test_initialise_is_idempotent(crypto_env):
    crypto, _, _sender_iid = crypto_env
    e1 = await crypto.initialise_for_space("sp-1")
    e2 = await crypto.initialise_for_space("sp-1")
    assert e1 == e2 == 0


async def test_rotate_epoch_increments_version(crypto_env):
    crypto, _, _sender_iid = crypto_env
    await crypto.initialise_for_space("sp-1")
    new_epoch = await crypto.rotate_epoch("sp-1")
    assert new_epoch == 1
    # Subsequent rotation also increments.
    assert await crypto.rotate_epoch("sp-1") == 2


async def test_encrypt_decrypt_roundtrip(crypto_env):
    crypto, _, _sender_iid = crypto_env
    await crypto.initialise_for_space("sp-1")
    epoch, ct = await crypto.encrypt("sp-1", b"the-payload-bytes")
    assert epoch == 0
    plaintext = await crypto.decrypt("sp-1", epoch, ct)
    assert plaintext == b"the-payload-bytes"


async def test_encrypt_without_init_raises_per_encryption_first_rule(crypto_env):
    """CLAUDE.md: never silently fall back to plaintext."""
    crypto, _, _sender_iid = crypto_env
    with pytest.raises(RuntimeError):
        await crypto.encrypt("sp-1", b"data")


async def test_decrypt_with_unknown_epoch_raises(crypto_env):
    crypto, _, _sender_iid = crypto_env
    await crypto.initialise_for_space("sp-1")
    _, ct = await crypto.encrypt("sp-1", b"data")
    with pytest.raises(RuntimeError):
        await crypto.decrypt("sp-1", epoch=99, ciphertext=ct)


async def test_decrypt_old_epoch_still_works_after_rotation(crypto_env):
    """Old epoch keys are kept indefinitely so historical content stays readable."""
    crypto, _, _sender_iid = crypto_env
    await crypto.initialise_for_space("sp-1")
    epoch0, ct0 = await crypto.encrypt("sp-1", b"epoch-0-content")
    await crypto.rotate_epoch("sp-1")
    epoch1, ct1 = await crypto.encrypt("sp-1", b"epoch-1-content")
    assert epoch0 == 0 and epoch1 == 1
    # Both old and new content decrypt correctly.
    assert await crypto.decrypt("sp-1", 0, ct0) == b"epoch-0-content"
    assert await crypto.decrypt("sp-1", 1, ct1) == b"epoch-1-content"


async def test_decrypt_rejects_malformed_ciphertext(crypto_env):
    crypto, _, _sender_iid = crypto_env
    await crypto.initialise_for_space("sp-1")
    with pytest.raises(ValueError):
        await crypto.decrypt("sp-1", 0, "no-colon-in-here")


async def test_get_current_epoch_when_uninitialised(crypto_env):
    crypto, _, _sender_iid = crypto_env
    assert await crypto.get_current_epoch("sp-1") is None


async def test_get_current_epoch_returns_latest(crypto_env):
    crypto, _, _sender_iid = crypto_env
    await crypto.initialise_for_space("sp-1")
    await crypto.rotate_epoch("sp-1")
    assert await crypto.get_current_epoch("sp-1") == 1


# ─── seal_for_gfs / unseal_from_gfs (§24.10) ──────────────────────────────


async def test_seal_for_gfs_roundtrip(crypto_env):
    """Sender + payload encrypt under the per-epoch key, are signed with
    this instance's identity seed, and decrypt + authenticate back. The
    sender is the fixture's registered member so unseal resolves its
    pubkey out of the box."""
    crypto, _, sender_iid = crypto_env
    await crypto.initialise_for_space("sp-1")
    sealed = await crypto.seal_for_gfs(
        space_id="sp-1",
        sender_instance_id=sender_iid,
        payload_json='{"hello":"world"}',
    )
    assert sealed.space_id == "sp-1"
    assert sealed.epoch == 0
    assert sealed.outer_signature  # signed
    # GFS sees only ciphertext for sender + payload.
    assert sender_iid not in sealed.encrypted_sender
    assert "hello" not in sealed.encrypted_payload

    unsealed = await crypto.unseal_from_gfs(sealed)
    assert unsealed.sender_instance_id == sender_iid
    assert unsealed.payload == {"hello": "world"}


async def test_seal_for_gfs_uses_latest_epoch(crypto_env):
    """After rotate_epoch, seal_for_gfs picks the new key."""
    crypto, _, sender_iid = crypto_env
    await crypto.initialise_for_space("sp-1")
    await crypto.rotate_epoch("sp-1")
    sealed = await crypto.seal_for_gfs(
        space_id="sp-1",
        sender_instance_id=sender_iid,
        payload_json='{"x":1}',
    )
    assert sealed.epoch == 1


async def test_seal_for_gfs_requires_identity_seed(crypto_env):
    """Without an identity seed the service can't sign the
    outer_signature, so it MUST refuse to seal rather than emit an
    unauthenticated envelope (fail-closed)."""
    _crypto, repo, _sender_iid = crypto_env
    # A service constructed without identity_seed (legacy/test shape).
    kek = _crypto._kek  # reuse the same KEK so the epoch key unwraps
    unsigned_svc = SpaceContentEncryption(repo, kek)
    await unsigned_svc.initialise_for_space("sp-1")
    with pytest.raises(RuntimeError, match="no identity_seed"):
        await unsigned_svc.seal_for_gfs(
            space_id="sp-1",
            sender_instance_id="whoever",
            payload_json="{}",
        )


async def test_unseal_from_gfs_rejects_forged_sender(crypto_env):
    """A key-holder forges an envelope claiming to be the registered
    member but signs with a DIFFERENT seed. unseal_from_gfs resolves
    the claimed member's real pubkey and the signature fails → raises."""
    from socialhome.federation.sealed_sender import (
        SealedSenderAuthError,
        seal_envelope,
    )

    crypto, _, sender_iid = crypto_env
    await crypto.initialise_for_space("sp-1")
    _epoch, raw_key = await crypto.export_current_key("sp-1")  # type: ignore[misc]
    attacker = generate_identity_keypair()
    forged = seal_envelope(
        space_id="sp-1",
        epoch=0,
        sender_instance_id=sender_iid,  # claim to be the member
        payload_json='{"text":"forged"}',
        space_content_key=raw_key,
        signer_seed=attacker.private_key,  # but sign with attacker's key
    )
    with pytest.raises(SealedSenderAuthError):
        await crypto.unseal_from_gfs(forged)


async def test_unseal_from_gfs_uses_explicit_lookup_override(crypto_env):
    """A caller may supply an explicit sender_pk_lookup (e.g. for a
    sender not yet in remote_instances)."""
    crypto, _, sender_iid = crypto_env
    await crypto.initialise_for_space("sp-1")
    sealed = await crypto.seal_for_gfs(
        space_id="sp-1",
        sender_instance_id=sender_iid,
        payload_json='{"k":"v"}',
    )
    # Resolve via the fixture instance's pubkey, supplied explicitly.
    inst = await crypto._fed_repo.get_instance(sender_iid)
    pub = bytes.fromhex(inst.remote_identity_pk)
    out = await crypto.unseal_from_gfs(
        sealed,
        sender_pk_lookup=lambda iid: pub if iid == sender_iid else None,
    )
    assert out.payload == {"k": "v"}


async def test_seal_for_gfs_unknown_space_raises(crypto_env):
    crypto, _, _sender_iid = crypto_env
    with pytest.raises(RuntimeError, match="no epoch key"):
        await crypto.seal_for_gfs(
            space_id="missing",
            sender_instance_id="me",
            payload_json="{}",
        )


async def test_unseal_unknown_epoch_raises(crypto_env):
    from socialhome.federation.sealed_sender import SealedEnvelope

    crypto, _, _sender_iid = crypto_env
    fake = SealedEnvelope(
        space_id="sp-1",
        epoch=99,
        encrypted_sender="a:b",
        encrypted_payload="a:b",
        outer_signature="sig",
    )
    with pytest.raises(RuntimeError, match="missing epoch"):
        await crypto.unseal_from_gfs(fake)


# ─── Sync chunks (§25.6 direct space sync) ────────────────────────────────


async def test_encrypt_decrypt_chunk_roundtrip(crypto_env):
    """A sync chunk AAD-binds to ``space_id:epoch:sync_id`` so it can
    only be decrypted with the matching tuple."""
    crypto, _, _sender_iid = crypto_env
    await crypto.initialise_for_space("sp-1")
    epoch, ciphertext = await crypto.encrypt_chunk(
        space_id="sp-1",
        sync_id="sync-abc",
        plaintext=b"chunk-bytes",
    )
    assert epoch == 0
    plain = await crypto.decrypt_chunk(
        space_id="sp-1",
        epoch=epoch,
        sync_id="sync-abc",
        ciphertext=ciphertext,
    )
    assert plain == b"chunk-bytes"


async def test_encrypt_chunk_without_init_raises(crypto_env):
    """Per the encryption-first rule, encrypt_chunk must NOT silently
    fall back to plaintext when no key has been minted."""
    crypto, _, _sender_iid = crypto_env
    with pytest.raises(RuntimeError, match="no key for space"):
        await crypto.encrypt_chunk(
            space_id="missing",
            sync_id="sync-1",
            plaintext=b"x",
        )


async def test_decrypt_chunk_unknown_epoch_raises(crypto_env):
    crypto, _, _sender_iid = crypto_env
    await crypto.initialise_for_space("sp-1")
    with pytest.raises(RuntimeError, match="missing epoch"):
        await crypto.decrypt_chunk(
            space_id="sp-1",
            epoch=99,
            sync_id="sync-1",
            ciphertext="aa:bb",
        )


async def test_decrypt_chunk_rejects_malformed_ciphertext(crypto_env):
    crypto, _, _sender_iid = crypto_env
    await crypto.initialise_for_space("sp-1")
    with pytest.raises(ValueError, match="Malformed space ciphertext"):
        await crypto.decrypt_chunk(
            space_id="sp-1",
            epoch=0,
            sync_id="sync-1",
            ciphertext="no-colon",
        )


async def test_decrypt_chunk_rejects_wrong_sync_id(crypto_env):
    """A chunk's AAD includes ``sync_id`` — replaying a chunk under a
    different sync_id must fail the tag check (§25.8.18)."""
    from cryptography.exceptions import InvalidTag

    crypto, _, _sender_iid = crypto_env
    await crypto.initialise_for_space("sp-1")
    epoch, ciphertext = await crypto.encrypt_chunk(
        space_id="sp-1",
        sync_id="sync-original",
        plaintext=b"hi",
    )
    with pytest.raises(InvalidTag):
        await crypto.decrypt_chunk(
            space_id="sp-1",
            epoch=epoch,
            sync_id="sync-different",
            ciphertext=ciphertext,
        )


# ─── verify_space_config error paths ──────────────────────────────────────


def test_verify_space_config_returns_false_for_bad_base64():
    """Malformed base64 signature is rejected without raising — caller
    just sees False so the §24.11 inbound pipeline can drop the event."""
    _, pk, _ = create_space_identity()
    assert (
        verify_space_config(
            b"payload",
            "not-valid-base64-!@#",
            space_public_key=pk,
        )
        is False
    )
