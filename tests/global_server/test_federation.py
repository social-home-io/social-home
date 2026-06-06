"""Tests for GfsFederationService — register, subscribe, publish with real SQLite."""

from __future__ import annotations

import base64
import json
from datetime import datetime, timedelta, timezone

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from socialhome.global_server.federation import GfsFederationService
from socialhome.global_server.repositories import SqliteGfsFederationRepo


# ── Helpers ────────────────────────────────────────────────────────────────────


def _make_keypair() -> tuple[bytes, bytes]:
    """Return (private_seed_bytes, public_key_bytes) for an Ed25519 keypair."""
    sk = Ed25519PrivateKey.generate()
    seed = sk.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    pk = sk.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return seed, pk


def _sign(seed: bytes, payload: dict) -> str:
    """Return a URL-safe base64 Ed25519 signature over the canonical JSON of *payload*."""
    canonical = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode(
        "utf-8"
    )
    sk = Ed25519PrivateKey.from_private_bytes(seed)
    sig = sk.sign(canonical)
    return base64.urlsafe_b64encode(sig).rstrip(b"=").decode("ascii")


# ── Fixtures ───────────────────────────────────────────────────────────────────


@pytest.fixture
async def svc(gfs_db):
    """A GfsFederationService backed by the shared GFS database fixture."""
    repo = SqliteGfsFederationRepo(gfs_db)
    return GfsFederationService(repo)


# ── Tests ──────────────────────────────────────────────────────────────────────


async def test_register_instance_succeeds(svc):
    """register_instance() persists the instance without raising."""
    await svc.register_instance("inst-1", "aa" * 32, "http://example.com/wh")
    spaces = await svc.list_spaces()
    assert isinstance(spaces, list)


async def test_register_instance_idempotent(svc):
    """Calling register_instance() twice with the same id updates the record."""
    await svc.register_instance("inst-dup", "aa" * 32, "http://old.example.com/wh")
    await svc.register_instance("inst-dup", "bb" * 32, "http://new.example.com/wh")


async def test_list_spaces_empty_initially(svc):
    """list_spaces() returns an empty list when no spaces exist."""
    spaces = await svc.list_spaces()
    assert spaces == []


async def test_subscribe_creates_space(svc):
    """subscribe() auto-creates a global_spaces row if needed."""
    await svc.register_instance("inst-a", "aa" * 32, "http://a.example.com/wh")
    await svc.subscribe("inst-a", "space-1")
    spaces = await svc.list_spaces()
    space_ids = [s.space_id for s in spaces]
    assert "space-1" in space_ids


async def test_subscribe_and_unsubscribe(svc):
    """subscribe() then unsubscribe() removes the subscription row."""
    await svc.register_instance("inst-b", "bb" * 32, "http://b.example.com/wh")
    await svc.subscribe("inst-b", "space-unsub")
    await svc.unsubscribe("inst-b", "space-unsub")
    await svc.subscribe("inst-b", "space-unsub")


async def test_subscribe_idempotent(svc):
    """subscribe() called twice for the same (instance, space) does not raise."""
    await svc.register_instance("inst-c", "cc" * 32, "http://c.example.com/wh")
    await svc.subscribe("inst-c", "space-idem")
    await svc.subscribe("inst-c", "space-idem")
    spaces = await svc.list_spaces()
    assert any(s.space_id == "space-idem" for s in spaces)


async def test_publish_unknown_instance_raises_permission_error(svc):
    """publish_event() raises PermissionError for an unregistered from_instance."""
    with pytest.raises(PermissionError, match="Unknown instance"):
        await svc.publish_event("space-1", "post.created", {"text": "hi"}, "ghost-inst")


async def test_publish_with_no_subscribers_returns_empty_list(svc):
    """publish_event() with zero subscribers returns an empty delivered list."""
    seed, pk = _make_keypair()
    await svc.register_instance("inst-pub", pk.hex(), "http://pub.example.com/wh")

    payload_dict = {
        "space_id": "space-nosubs",
        "event_type": "post.created",
        "payload": {"text": "hello"},
        "from_instance": "inst-pub",
    }
    sig = _sign(seed, payload_dict)
    delivered = await svc.publish_event(
        "space-nosubs",
        "post.created",
        {"text": "hello"},
        "inst-pub",
        sig,
    )
    assert delivered == []


async def test_publish_invalid_signature_raises_permission_error(svc):
    """publish_event() rejects a bad signature with PermissionError."""
    _, pk = _make_keypair()
    await svc.register_instance("inst-badsig", pk.hex(), "http://badsig.example.com/wh")
    with pytest.raises(PermissionError, match="Invalid Ed25519 signature"):
        await svc.publish_event(
            "space-sig",
            "post.created",
            {"text": "hi"},
            "inst-badsig",
            signature="invalidsignatureXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX",
        )


async def test_publish_without_signature_still_relays(svc):
    """publish_event() with an empty signature skips verification and relays."""
    await svc.register_instance("inst-nosig", "dd" * 32, "http://nosig.example.com/wh")
    delivered = await svc.publish_event(
        "space-nosig",
        "ping",
        {},
        "inst-nosig",
        signature="",
    )
    assert isinstance(delivered, list)


# ── update_instance (signed display-name change) ────────────────────────────────


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def test_update_instance_renames_with_valid_signature(svc):
    """update_instance() persists a new display_name when the signature
    verifies against the registered public key and the ts is fresh."""
    seed, pk = _make_keypair()
    await svc.register_instance(
        "inst-rename", pk.hex(), "http://r.example.com/wh", display_name="Old"
    )
    ts = _now_iso()
    sig = _sign(
        seed,
        {"instance_id": "inst-rename", "display_name": "New Home", "ts": ts},
    )
    await svc.update_instance("inst-rename", "New Home", ts, sig)

    inst = await svc._repo.get_instance("inst-rename")
    assert inst is not None
    assert inst.display_name == "New Home"


async def test_update_instance_unknown_raises_permission_error(svc):
    """An instance the GFS never registered cannot rename — PermissionError."""
    ts = _now_iso()
    with pytest.raises(PermissionError, match="Unknown instance"):
        await svc.update_instance("ghost-inst", "Whatever", ts, "AAAA")


async def test_update_instance_bad_signature_raises_permission_error(svc):
    """A forged / mismatched signature is rejected with PermissionError."""
    _, pk = _make_keypair()
    await svc.register_instance("inst-badsig2", pk.hex(), "http://b.example.com/wh")
    other_seed, _ = _make_keypair()
    ts = _now_iso()
    sig = _sign(
        other_seed,
        {"instance_id": "inst-badsig2", "display_name": "Hijack", "ts": ts},
    )
    with pytest.raises(PermissionError, match="Invalid Ed25519 signature"):
        await svc.update_instance("inst-badsig2", "Hijack", ts, sig)


async def test_update_instance_empty_signature_raises_permission_error(svc):
    """The signature is REQUIRED (unlike publish_space's optional branch)."""
    _, pk = _make_keypair()
    await svc.register_instance("inst-nosig2", pk.hex(), "http://n.example.com/wh")
    ts = _now_iso()
    with pytest.raises(PermissionError):
        await svc.update_instance("inst-nosig2", "Name", ts, "")


async def test_update_instance_stale_timestamp_raises_permission_error(svc):
    """A ts older than the 300s replay window is rejected."""
    seed, pk = _make_keypair()
    await svc.register_instance("inst-stale", pk.hex(), "http://s.example.com/wh")
    ts = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    sig = _sign(
        seed,
        {"instance_id": "inst-stale", "display_name": "Late", "ts": ts},
    )
    with pytest.raises(PermissionError, match="Stale timestamp"):
        await svc.update_instance("inst-stale", "Late", ts, sig)


async def test_update_instance_unparseable_timestamp_raises_permission_error(svc):
    """A ts that isn't ISO 8601 is rejected (treated as stale/invalid)."""
    seed, pk = _make_keypair()
    await svc.register_instance("inst-badts", pk.hex(), "http://t.example.com/wh")
    ts = "not-a-timestamp"
    sig = _sign(
        seed,
        {"instance_id": "inst-badts", "display_name": "X", "ts": ts},
    )
    with pytest.raises(PermissionError):
        await svc.update_instance("inst-badts", "X", ts, sig)


async def test_update_instance_empty_name_raises_value_error(svc):
    """An empty (after-strip) display_name is rejected with ValueError."""
    seed, pk = _make_keypair()
    await svc.register_instance("inst-empty", pk.hex(), "http://e.example.com/wh")
    ts = _now_iso()
    sig = _sign(
        seed,
        {"instance_id": "inst-empty", "display_name": "   ", "ts": ts},
    )
    with pytest.raises(ValueError, match="1-80"):
        await svc.update_instance("inst-empty", "   ", ts, sig)


async def test_update_instance_overlong_name_raises_value_error(svc):
    """A >80-char display_name is rejected with ValueError."""
    seed, pk = _make_keypair()
    await svc.register_instance("inst-long", pk.hex(), "http://l.example.com/wh")
    ts = _now_iso()
    name = "y" * 81
    sig = _sign(
        seed,
        {"instance_id": "inst-long", "display_name": name, "ts": ts},
    )
    with pytest.raises(ValueError, match="1-80"):
        await svc.update_instance("inst-long", name, ts, sig)
