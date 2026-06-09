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


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


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


async def _publish_known_space(
    svc, seed: bytes, *, owning_instance: str, space_id: str
):
    """Publish a minimal active space so a subscribe target exists."""
    body = {
        "space_id": space_id,
        "owning_instance": owning_instance,
        "name": "Known",
        "description": "",
        "about_markdown": "",
        "cover_url": "",
        "icon_url": "",
        "min_age": 0,
        "target_audience": "all",
        "accent_color": "#D2542A",
        "primary_color": "#D2542A",
    }
    sig = _sign(seed, body)
    await svc.publish_space(
        space_id=space_id,
        owning_instance=owning_instance,
        name="Known",
        signature=sig,
    )


async def test_subscribe_self_signed_known_space_succeeds(svc):
    """subscribe() accepts a self-signed request for an already-published space."""
    seed, pk = _make_keypair()
    await svc.register_instance(
        "inst-a", pk.hex(), "http://a.example.com/wh", auto_accept=True
    )
    await _publish_known_space(svc, seed, owning_instance="inst-a", space_id="space-1")
    ts = _now_iso()
    sig = _sign(seed, {"instance_id": "inst-a", "space_id": "space-1", "ts": ts})
    await svc.subscribe("inst-a", "space-1", ts, sig)
    subs = await svc._repo.list_subscribers("space-1")
    assert any(s.instance_id == "inst-a" for s in subs)


async def test_subscribe_unknown_instance_rejected(svc):
    """An instance the GFS never registered cannot subscribe — PermissionError."""
    ts = _now_iso()
    with pytest.raises(PermissionError, match="Unknown instance"):
        await svc.subscribe("ghost-inst", "space-1", ts, "AAAA")


async def test_subscribe_no_signature_rejected(svc):
    """The signature is mandatory — an empty signature is a PermissionError."""
    _, pk = _make_keypair()
    await svc.register_instance("inst-nos", pk.hex(), "http://n.example.com/wh")
    ts = _now_iso()
    with pytest.raises(PermissionError):
        await svc.subscribe("inst-nos", "space-1", ts, "")


async def test_subscribe_signature_from_other_instance_rejected(svc):
    """A signature produced by a different key fails verification."""
    seed_a, pk_a = _make_keypair()
    other_seed, _ = _make_keypair()
    await svc.register_instance(
        "inst-x", pk_a.hex(), "http://x.example.com/wh", auto_accept=True
    )
    await _publish_known_space(
        svc, seed_a, owning_instance="inst-x", space_id="space-x"
    )
    ts = _now_iso()
    # Sign with the wrong key for inst-x's own id.
    sig = _sign(other_seed, {"instance_id": "inst-x", "space_id": "space-x", "ts": ts})
    with pytest.raises(PermissionError, match="Invalid Ed25519 signature"):
        await svc.subscribe("inst-x", "space-x", ts, sig)


async def test_subscribe_cannot_sign_for_another_instance(svc):
    """A caller can only subscribe *itself*: signing inst-b's body with
    inst-a's key fails because the signature is checked against the
    instance_id in the body (inst-b's registered key)."""
    seed_a, pk_a = _make_keypair()
    seed_b, pk_b = _make_keypair()
    await svc.register_instance(
        "inst-a2", pk_a.hex(), "http://a.example.com/wh", auto_accept=True
    )
    await svc.register_instance("inst-b2", pk_b.hex(), "http://b.example.com/wh")
    await _publish_known_space(
        svc, seed_a, owning_instance="inst-a2", space_id="space-ab"
    )
    ts = _now_iso()
    # inst-a2 tries to subscribe inst-b2 (claims instance_id=inst-b2) — must
    # sign as inst-b2 to pass, which it can't.
    sig = _sign(seed_a, {"instance_id": "inst-b2", "space_id": "space-ab", "ts": ts})
    with pytest.raises(PermissionError, match="Invalid Ed25519 signature"):
        await svc.subscribe("inst-b2", "space-ab", ts, sig)


async def test_subscribe_stale_timestamp_rejected(svc):
    """A ts outside the ±300 s replay window is rejected."""
    seed, pk = _make_keypair()
    await svc.register_instance(
        "inst-stale-sub", pk.hex(), "http://s.example.com/wh", auto_accept=True
    )
    await _publish_known_space(
        svc, seed, owning_instance="inst-stale-sub", space_id="space-stale"
    )
    ts = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    sig = _sign(
        seed, {"instance_id": "inst-stale-sub", "space_id": "space-stale", "ts": ts}
    )
    with pytest.raises(PermissionError, match="Stale timestamp"):
        await svc.subscribe("inst-stale-sub", "space-stale", ts, sig)


async def test_subscribe_unknown_space_rejected(svc):
    """A subscribe to a space the GFS has never seen is rejected — no
    auto-create of a pending row from an unauthenticated demand signal."""
    seed, pk = _make_keypair()
    await svc.register_instance("inst-d", pk.hex(), "http://d.example.com/wh")
    ts = _now_iso()
    sig = _sign(seed, {"instance_id": "inst-d", "space_id": "space-ghost", "ts": ts})
    with pytest.raises(PermissionError, match="not published"):
        await svc.subscribe("inst-d", "space-ghost", ts, sig)
    # And no row was minted.
    assert await svc.get_space("space-ghost") is None


async def test_subscribe_and_unsubscribe(svc):
    """subscribe() then unsubscribe() removes the subscription row."""
    seed, pk = _make_keypair()
    await svc.register_instance(
        "inst-b", pk.hex(), "http://b.example.com/wh", auto_accept=True
    )
    await _publish_known_space(
        svc, seed, owning_instance="inst-b", space_id="space-unsub"
    )
    ts = _now_iso()
    sig = _sign(seed, {"instance_id": "inst-b", "space_id": "space-unsub", "ts": ts})
    await svc.subscribe("inst-b", "space-unsub", ts, sig)
    await svc.unsubscribe("inst-b", "space-unsub")
    ts2 = _now_iso()
    sig2 = _sign(seed, {"instance_id": "inst-b", "space_id": "space-unsub", "ts": ts2})
    await svc.subscribe("inst-b", "space-unsub", ts2, sig2)


async def test_subscribe_idempotent(svc):
    """subscribe() called twice for the same (instance, space) does not raise."""
    seed, pk = _make_keypair()
    await svc.register_instance(
        "inst-c", pk.hex(), "http://c.example.com/wh", auto_accept=True
    )
    await _publish_known_space(
        svc, seed, owning_instance="inst-c", space_id="space-idem"
    )
    ts = _now_iso()
    sig = _sign(seed, {"instance_id": "inst-c", "space_id": "space-idem", "ts": ts})
    await svc.subscribe("inst-c", "space-idem", ts, sig)
    ts2 = _now_iso()
    sig2 = _sign(seed, {"instance_id": "inst-c", "space_id": "space-idem", "ts": ts2})
    await svc.subscribe("inst-c", "space-idem", ts2, sig2)
    subs = await svc._repo.list_subscribers("space-idem")
    assert sum(1 for s in subs if s.instance_id == "inst-c") == 1


async def test_publish_unknown_instance_raises_permission_error(svc):
    """publish_event() raises PermissionError for an unregistered from_instance."""
    with pytest.raises(PermissionError, match="Unknown instance"):
        await svc.publish_event("space-1", "post.created", {"text": "hi"}, "ghost-inst")


async def test_publish_with_no_subscribers_returns_empty_list(svc):
    """publish_event() with zero subscribers returns an empty delivered list."""
    seed, pk = _make_keypair()
    await svc.register_instance(
        "inst-pub", pk.hex(), "http://pub.example.com/wh", auto_accept=True
    )
    # The space must exist and be owned by the publisher.
    await _publish_known_space(
        svc, seed, owning_instance="inst-pub", space_id="space-nosubs"
    )

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


async def test_publish_event_unknown_space_rejected(svc):
    """publish_event() rejects an event for a space the GFS never saw — no
    auto-creation of an ownership row from an event (mirrors subscribe)."""
    seed, pk = _make_keypair()
    await svc.register_instance(
        "inst-unk", pk.hex(), "http://unk.example.com/wh", auto_accept=True
    )
    sig = _sign(
        seed,
        {
            "space_id": "space-never",
            "event_type": "post.created",
            "payload": {"text": "hi"},
            "from_instance": "inst-unk",
        },
    )
    with pytest.raises(PermissionError, match="not published"):
        await svc.publish_event(
            "space-never", "post.created", {"text": "hi"}, "inst-unk", sig
        )


async def test_publish_event_from_non_owner_rejected(svc):
    """Only the space's owning instance may relay events for it: a registered
    peer that is not the owner is rejected even with a valid signature."""
    owner_seed, owner_pk = _make_keypair()
    other_seed, other_pk = _make_keypair()
    await svc.register_instance(
        "owner-e", owner_pk.hex(), "http://owner.example.com/wh", auto_accept=True
    )
    await svc.register_instance(
        "other-e", other_pk.hex(), "http://other.example.com/wh", auto_accept=True
    )
    await _publish_known_space(
        svc, owner_seed, owning_instance="owner-e", space_id="space-owned"
    )
    # other-e signs a valid event for owner-e's space — must be rejected.
    sig = _sign(
        other_seed,
        {
            "space_id": "space-owned",
            "event_type": "post.created",
            "payload": {"text": "hi"},
            "from_instance": "other-e",
        },
    )
    with pytest.raises(PermissionError, match="not the owner"):
        await svc.publish_event(
            "space-owned", "post.created", {"text": "hi"}, "other-e", sig
        )


async def test_publish_event_from_owner_succeeds(svc):
    """The owning instance can relay events for its own space."""
    seed, pk = _make_keypair()
    await svc.register_instance(
        "owner-ok", pk.hex(), "http://ok.example.com/wh", auto_accept=True
    )
    await _publish_known_space(
        svc, seed, owning_instance="owner-ok", space_id="space-ok"
    )
    sig = _sign(
        seed,
        {
            "space_id": "space-ok",
            "event_type": "post.created",
            "payload": {"text": "hi"},
            "from_instance": "owner-ok",
        },
    )
    delivered = await svc.publish_event(
        "space-ok", "post.created", {"text": "hi"}, "owner-ok", sig
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


async def test_publish_event_empty_signature_rejected(svc):
    """publish_event() now REQUIRES a signature — empty is a PermissionError."""
    _, pk = _make_keypair()
    await svc.register_instance("inst-nosig", pk.hex(), "http://nosig.example.com/wh")
    with pytest.raises(PermissionError):
        await svc.publish_event(
            "space-nosig",
            "ping",
            {},
            "inst-nosig",
            signature="",
        )


async def test_publish_event_malformed_signature_rejected(svc):
    """A signature that isn't valid base64url is rejected with PermissionError."""
    _, pk = _make_keypair()
    await svc.register_instance("inst-mal", pk.hex(), "http://mal.example.com/wh")
    with pytest.raises(PermissionError, match="Invalid Ed25519 signature"):
        await svc.publish_event(
            "space-mal",
            "ping",
            {},
            "inst-mal",
            signature="!!!not-base64!!!",
        )


# ── publish_space (signed space-metadata publish) ───────────────────────────────


def _publish_space_args(owning_instance: str, space_id: str) -> dict:
    return {
        "space_id": space_id,
        "owning_instance": owning_instance,
        "name": "My Space",
        "description": "",
        "about_markdown": "",
        "cover_url": "",
        "icon_url": "",
        "min_age": 0,
        "target_audience": "all",
        "accent_color": "#D2542A",
        "primary_color": "#D2542A",
    }


async def test_publish_space_valid_signature_succeeds(svc):
    """publish_space() persists the row when the signature verifies."""
    seed, pk = _make_keypair()
    await svc.register_instance(
        "inst-ps", pk.hex(), "http://ps.example.com/wh", auto_accept=True
    )
    sig = _sign(seed, _publish_space_args("inst-ps", "space-ps"))
    space = await svc.publish_space(
        space_id="space-ps",
        owning_instance="inst-ps",
        name="My Space",
        signature=sig,
    )
    assert space.space_id == "space-ps"
    assert await svc.get_space("space-ps") is not None


async def test_publish_space_empty_signature_rejected(svc):
    """publish_space() now REQUIRES a signature — empty is a PermissionError."""
    _, pk = _make_keypair()
    await svc.register_instance("inst-ps2", pk.hex(), "http://ps2.example.com/wh")
    with pytest.raises(PermissionError):
        await svc.publish_space(
            space_id="space-ps2",
            owning_instance="inst-ps2",
            name="My Space",
            signature="",
        )


async def test_publish_space_invalid_signature_rejected(svc):
    """A forged signature is rejected with PermissionError."""
    _, pk = _make_keypair()
    other_seed, _ = _make_keypair()
    await svc.register_instance("inst-ps3", pk.hex(), "http://ps3.example.com/wh")
    sig = _sign(other_seed, _publish_space_args("inst-ps3", "space-ps3"))
    with pytest.raises(PermissionError, match="Invalid Ed25519 signature"):
        await svc.publish_space(
            space_id="space-ps3",
            owning_instance="inst-ps3",
            name="My Space",
            signature=sig,
        )


async def test_publish_space_malformed_signature_rejected(svc):
    """A signature that isn't valid base64url is rejected with PermissionError."""
    _, pk = _make_keypair()
    await svc.register_instance("inst-ps4", pk.hex(), "http://ps4.example.com/wh")
    with pytest.raises(PermissionError, match="Invalid Ed25519 signature"):
        await svc.publish_space(
            space_id="space-ps4",
            owning_instance="inst-ps4",
            name="My Space",
            signature="!!!not-base64!!!",
        )


async def test_publish_space_cannot_hijack_another_owners_space(svc):
    """A registered peer can't seize a space_id another instance already owns.

    space_id is a public, owner-chosen UUID (it travels in discovery links),
    so a malicious-but-registered peer that learns it could otherwise re-publish
    the row with owning_instance=itself — validly signed by its OWN key — and
    hijack the listing. The owner is immutable after first publish: a publish
    whose owning_instance differs from the stored owner is a PermissionError,
    even with a signature valid for the attacker's own key.
    """
    owner_seed, owner_pk = _make_keypair()
    attacker_seed, attacker_pk = _make_keypair()
    await svc.register_instance(
        "owner-inst", owner_pk.hex(), "http://owner.example.com/wh", auto_accept=True
    )
    await svc.register_instance(
        "attacker-inst",
        attacker_pk.hex(),
        "http://attacker.example.com/wh",
        auto_accept=True,
    )
    # Legit owner establishes the space.
    sig = _sign(owner_seed, _publish_space_args("owner-inst", "shared-space"))
    await svc.publish_space(
        space_id="shared-space",
        owning_instance="owner-inst",
        name="My Space",
        signature=sig,
    )
    # Attacker signs a publish for the SAME space_id with itself as owner,
    # validly signed by its own key — must still be rejected.
    forged = _sign(attacker_seed, _publish_space_args("attacker-inst", "shared-space"))
    with pytest.raises(PermissionError, match="owned by another instance"):
        await svc.publish_space(
            space_id="shared-space",
            owning_instance="attacker-inst",
            name="My Space",
            signature=forged,
        )
    # The original owner is untouched.
    row = await svc.get_space("shared-space")
    assert row is not None
    assert row.owning_instance == "owner-inst"


async def test_publish_space_owner_can_refresh_own_space(svc):
    """The established owner can re-publish its own space (idempotent refresh)."""
    seed, pk = _make_keypair()
    await svc.register_instance(
        "owner2-inst", pk.hex(), "http://owner2.example.com/wh", auto_accept=True
    )
    sig = _sign(seed, _publish_space_args("owner2-inst", "refresh-space"))
    await svc.publish_space(
        space_id="refresh-space",
        owning_instance="owner2-inst",
        name="My Space",
        signature=sig,
    )
    # Same owner publishes again — allowed.
    sig2 = _sign(seed, _publish_space_args("owner2-inst", "refresh-space"))
    await svc.publish_space(
        space_id="refresh-space",
        owning_instance="owner2-inst",
        name="My Space",
        signature=sig2,
    )
    row = await svc.get_space("refresh-space")
    assert row is not None and row.owning_instance == "owner2-inst"


# ── update_instance (signed display-name change) ────────────────────────────────


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
