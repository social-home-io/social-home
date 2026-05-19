"""Shared visibility-lookup helper for outbound federation services.

The per-pair user-visibility toggle in ConnectionDetail
(``PATCH /api/pairing/connections/{instance_id}/visible-users``) writes
into ``peer_user_visibility``. This module exposes the one read every
outbound service needs: "which of our local users are hidden from this
specific peer?"

The shape is a set of user_ids per peer so an outbound that fans one
event to many users (e.g. ``USERS_SYNC``, ``DM_HISTORY_CHUNK``) can do
one DB read and filter with set membership. The companion shape — one
read per (peer, user) pair — would be N lookups for the same batch; we
prefer the bulk read.

Fail-soft: a missing repo (``None``) or a repo error returns the empty
set, i.e. default-visible. The contract matches the existing
``ProfileFederationOutbound._on_updated`` semantics — visibility lookup
must never block federation outbound on a transient infra failure.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..repositories.peer_user_visibility_repo import (
        AbstractPeerUserVisibilityRepo,
    )

log = logging.getLogger(__name__)


async def hidden_for_peer(
    repo: "AbstractPeerUserVisibilityRepo | None",
    peer_id: str,
) -> frozenset[str]:
    """Return the set of local user_ids hidden from ``peer_id``.

    Returns the empty set when ``repo`` is ``None`` (back-compat for
    test wiring that doesn't inject the repo) or when the repo raises
    (transient infra failure → default-visible, not fail-closed).
    """
    if repo is None:
        return frozenset()
    try:
        return await repo.hidden_user_ids_for_peer(peer_id)
    except Exception as exc:  # pragma: no cover — defensive
        log.debug(
            "_visibility.hidden_for_peer: lookup failed for %s: %s",
            peer_id,
            exc,
        )
        return frozenset()
