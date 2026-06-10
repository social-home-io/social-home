"""Tests for the canonical per-author signing bytes helper."""

from __future__ import annotations

from datetime import datetime, timezone

from socialhome.crypto import (
    b64url_decode,
    b64url_encode,
    derive_user_id,
    generate_identity_keypair,
    sign_ed25519,
    verify_ed25519,
)
from socialhome.domain.post import LocationData, Post, PostType
from socialhome.services.space_public_author import (
    author_signing_bytes,
    build_signed_author_inner,
    verify_signed_author_inner,
)


def _inner(**over) -> dict:
    base = {
        "post_id": "p1",
        "space_id": "sp",
        "author_user_id": "u1",
        "author_pk": "ab" * 32,
        "author_username": "bob",
        "type": "text",
        "content": "hi",
        "media_url": None,
        "image_urls": [],
        "created_at": "2026-06-10T00:00:00+00:00",
        "location": None,
        "origin_instance_id": "remote.home",
    }
    base.update(over)
    return base


def test_domain_separated_prefix():
    assert author_signing_bytes(_inner()).startswith(b"space-post-author:v1:")


def test_author_sig_excluded_from_signed_bytes():
    """Presence of ``author_sig`` must not change the signing bytes."""
    without = author_signing_bytes(_inner())
    with_sig = author_signing_bytes(_inner(author_sig="anything"))
    assert without == with_sig


def test_deterministic_and_order_independent():
    a = author_signing_bytes(_inner())
    # Same logical content, different dict insertion order.
    reordered = {}
    for k in reversed(list(_inner().keys())):
        reordered[k] = _inner()[k]
    b = author_signing_bytes(reordered)
    assert a == b


def test_changing_an_attributable_field_changes_bytes():
    base = author_signing_bytes(_inner())
    assert author_signing_bytes(_inner(content="tampered")) != base
    assert author_signing_bytes(_inner(author_user_id="someone-else")) != base
    assert author_signing_bytes(_inner(author_pk="cd" * 32)) != base


def test_missing_optional_field_defaults_to_none():
    full = _inner()
    full.pop("media_url")
    # media_url absent should equal media_url=None.
    assert author_signing_bytes(full) == author_signing_bytes(_inner(media_url=None))


def _post(**over) -> Post:
    base = dict(
        id="p1",
        author="u1",
        type=PostType.TEXT,
        created_at=datetime(2026, 6, 10, tzinfo=timezone.utc),
        content="hi",
        media_url=None,
        image_urls=("a.jpg", "b.jpg"),
        hidden_from_feed=False,
        location=None,
    )
    base.update(over)
    return Post(**base)


def test_build_signed_author_inner_fields_and_roundtrip():
    kp = generate_identity_keypair()
    post = _post()
    inner = build_signed_author_inner(
        post=post,
        space_id="sp",
        author_username="bob",
        author_pk=kp.public_key,
        author_identity_seed=kp.private_key,
        origin_instance_id="origin.home",
    )
    assert inner["post_id"] == "p1"
    assert inner["space_id"] == "sp"
    assert inner["author_user_id"] == "u1"
    assert inner["author_pk"] == kp.public_key.hex()
    assert inner["author_username"] == "bob"
    assert inner["type"] == "text"
    assert inner["content"] == "hi"
    assert inner["media_url"] is None
    assert inner["image_urls"] == ["a.jpg", "b.jpg"]
    assert inner["created_at"] == "2026-06-10T00:00:00+00:00"
    assert inner["hidden_from_feed"] is False
    assert inner["origin_instance_id"] == "origin.home"
    assert "location" not in inner
    # author_sig verifies against the author_pk via the canonical bytes.
    assert verify_ed25519(
        kp.public_key,
        author_signing_bytes(inner),
        b64url_decode(inner["author_sig"]),
    )


def test_build_signed_author_inner_with_location_roundtrip():
    kp = generate_identity_keypair()
    post = _post(
        type=PostType.LOCATION,
        location=LocationData(lat=1.2345, lon=6.789, label="home"),
    )
    inner = build_signed_author_inner(
        post=post,
        space_id="sp",
        author_username="bob",
        author_pk=kp.public_key,
        author_identity_seed=kp.private_key,
        origin_instance_id="origin.home",
    )
    assert inner["location"] == {"lat": 1.2345, "lon": 6.789, "label": "home"}
    assert verify_ed25519(
        kp.public_key,
        author_signing_bytes(inner),
        b64url_decode(inner["author_sig"]),
    )


def _signed_inner(**over):
    """A well-formed, self-certifying, author-signed inner for verify tests."""
    kp = generate_identity_keypair()
    username = "bob"
    inner = build_signed_author_inner(
        post=_post(author=derive_user_id(kp.public_key, username)),
        space_id="sp",
        author_username=username,
        author_pk=kp.public_key,
        author_identity_seed=kp.private_key,
        origin_instance_id="origin.home",
    )
    inner.update(over)
    return kp, inner


def test_verify_signed_author_inner_accepts_valid():
    _kp, inner = _signed_inner()
    assert verify_signed_author_inner(inner) is True


def test_verify_signed_author_inner_rejects_missing_identity_fields():
    _kp, inner = _signed_inner()
    for field_name in ("post_id", "author_user_id", "author_pk", "author_username"):
        bad = dict(inner)
        bad.pop(field_name)
        assert verify_signed_author_inner(bad) is False


def test_verify_signed_author_inner_rejects_self_cert_mismatch():
    # author_user_id does not derive from (author_pk, author_username).
    _kp, inner = _signed_inner(author_user_id="not-the-right-id")
    assert verify_signed_author_inner(inner) is False


def test_verify_signed_author_inner_rejects_tampered_content():
    # author_sig was computed over the original content; tampering breaks it.
    _kp, inner = _signed_inner()
    inner["content"] = "tampered"
    assert verify_signed_author_inner(inner) is False


def test_verify_signed_author_inner_rejects_missing_sig():
    _kp, inner = _signed_inner()
    inner.pop("author_sig")
    assert verify_signed_author_inner(inner) is False


def test_verify_signed_author_inner_rejects_malformed_sig():
    _kp, inner = _signed_inner()
    inner["author_sig"] = "!!!not base64!!!"
    assert verify_signed_author_inner(inner) is False


def test_verify_signed_author_inner_rejects_wrong_signing_key():
    # author_pk is the real author, but the sig was made by a different key.
    impostor = generate_identity_keypair()
    kp, inner = _signed_inner()
    inner["author_sig"] = b64url_encode(
        sign_ed25519(impostor.private_key, author_signing_bytes(inner))
    )
    assert verify_signed_author_inner(inner) is False


def test_verify_signed_author_inner_fail_closed_on_non_dict():
    assert verify_signed_author_inner({}) is False


def test_hidden_from_feed_is_signed_and_roundtrips():
    """``hidden_from_feed`` is bound to the author signature so a relayer
    cannot flip feed visibility undetectably."""
    kp = generate_identity_keypair()
    username = "bob"
    inner = build_signed_author_inner(
        post=_post(author=derive_user_id(kp.public_key, username)),
        space_id="sp",
        author_username=username,
        author_pk=kp.public_key,
        author_identity_seed=kp.private_key,
        origin_instance_id="origin.home",
    )
    # Round-trips with hidden_from_feed signed.
    assert verify_signed_author_inner(inner) is True
    # Flipping hidden_from_feed after signing breaks verification.
    inner["hidden_from_feed"] = not inner["hidden_from_feed"]
    assert verify_signed_author_inner(inner) is False
