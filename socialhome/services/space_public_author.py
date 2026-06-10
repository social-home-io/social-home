"""Canonical per-author signing bytes for the public space-content relay.

The public space relay (``space_public_outbound`` / ``space_public_inbound``)
carries TWO independent signatures:

* the **space-authority** signature on the *outer* envelope — produced by the
  relaying seed-holder's space seed, verified against the TOFU-pinned space
  public key (proves the relay is authorised, lets the GFS gate the fan-out
  while staying content-blind), and
* a **per-author** signature on the *inner* (encrypted) content — produced by
  the author's *household identity seed*, verified against ``author_pk``
  (proves the named author actually wrote the post).

Without the per-author signature, attribution rests only on the authority
signature + a ``derive_user_id(author_pk, username) == author_user_id``
self-cert. Both ``author_pk`` and ``author_user_id`` are PUBLIC, so a
seed-holder could attribute any post to any member. The per-author signature
closes that hole — it can only be produced by a household holding the author's
identity seed.

This module owns the ONE canonical, domain-separated message both sides sign /
verify over so the two never drift. The signed message covers every
attributable field of the inner payload and EXCLUDES ``author_sig`` itself.
"""

from __future__ import annotations

import json

#: Domain-separation prefix — distinguishes these signing bytes from every
#: other Ed25519 signature in the system (envelope authority sig, user
#: assertions, moment envelopes, …). Bump the suffix on any wire change.
_AUTHOR_SIG_DOMAIN: bytes = b"space-post-author:v1:"

#: The inner-payload fields the author signs over — everything attributable.
#: ``author_pk`` is included so a swap of (pk, sig) pair can't re-target the
#: post; ``author_sig`` itself is excluded (it signs the rest). Anything not
#: listed (e.g. ``hidden_from_feed``) is presentation, not attribution.
_SIGNED_FIELDS: tuple[str, ...] = (
    "post_id",
    "space_id",
    "author_user_id",
    "author_pk",
    "author_username",
    "type",
    "content",
    "media_url",
    "image_urls",
    "created_at",
    "location",
    "origin_instance_id",
)


def author_signing_bytes(inner: dict) -> bytes:
    """Return the canonical, domain-separated bytes the author signs / the
    receiver verifies for one relayed public-space post.

    ``inner`` is the decrypted inner payload (with or without ``author_sig`` —
    it is never read). Missing fields default to ``None`` so producer and
    consumer agree even when an optional field is absent. The body is compact,
    sorted JSON so the encoding is deterministic across both sides.
    """
    body = {k: inner.get(k) for k in _SIGNED_FIELDS}
    canonical = json.dumps(body, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return _AUTHOR_SIG_DOMAIN + canonical
