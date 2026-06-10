"""Tests for the canonical per-author signing bytes helper."""

from __future__ import annotations

from socialhome.services.space_public_author import author_signing_bytes


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
