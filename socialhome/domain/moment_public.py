"""Domain types for the public-Momentum-via-GFS pillar (§Momentum-public).

The Momentum pillar (``socialhome/domain/moment.py``) federates
moments only across paired households (3-hop relay, max). This module
adds an opt-in *public* layer brokered by a GFS:

* A user **registers** themself on one or more GFSes
  (:class:`MomentPublicRegistration`).
* Other instances discover registered users via the GFS directory,
  then their users **follow** specific authors
  (:class:`MomentPublicFollow`).
* When a registered author posts, the moment fans out (a) via the
  existing household federation AND (b) as a signed
  :class:`MomentPublicEnvelope` over the persistent SH↔GFS WebSocket.
  The GFS pushes the same envelope to every follower instance.
* Recipients verify the envelope's Ed25519 signature against the
  ``followed_instance_pk`` they cached at follow-time, persist the
  moment with ``received_via='gfs'``, and — critically — **do not
  re-relay** it to their own paired peers.

The encryption-first rule (§25.8.21) is preserved by the GFS holding
plaintext only in memory during fan-out (never on disk) and by the
recipient's signature check ensuring authorship can't be forged.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True, frozen=True)
class MomentPublicRegistration:
    """Author-side row: which GFS this user has opted into.

    One row per ``(user_id, gfs_id)``. ``default_share`` lets the
    author flip whether *every* moment is public or whether they want
    to opt in per-moment in the composer.
    """

    user_id: str
    gfs_id: str
    registered_at: str
    default_share: bool = True


@dataclass(slots=True, frozen=True)
class MomentPublicFollow:
    """Follower-side row: who this user follows publicly via which GFS.

    Caches the followed user's home-instance public key so every
    inbound public-moment envelope can be Ed25519-verified locally —
    no round-trip back to the GFS, no second source-of-truth.
    """

    follower_user_id: str
    followed_user_id: str
    gfs_id: str
    followed_instance_pk: str
    followed_username: str
    followed_display_name: str
    created_at: str


@dataclass(slots=True, frozen=True)
class MomentPublicEnvelope:
    """Signed wire envelope for a public-moment fan-out frame.

    Sent by the author's instance over its persistent WS to each GFS
    on which the author is registered, and forwarded verbatim by the
    GFS to every follower instance. The recipient validates
    ``signature`` over the canonical-JSON of every other field using
    the cached ``followed_instance_pk``.
    """

    moment_id: str
    author_user_id: str
    author_username: str
    author_display_name: str
    content: str
    media_url: str | None
    media_type: str | None
    duration_ms: int | None
    parent_moment_id: str | None
    origin_instance_id: str
    created_at: str
    expires_at: str
    #: Hex-encoded Ed25519 over the canonical-JSON of all other fields.
    signature: str
