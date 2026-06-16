"""Domain exceptions for the move-out primitive (MO-1)."""


class StaleMoveLink(Exception):
    """A move-link/redirect whose ``issued_at`` is not strictly newer than the
    one already recorded for this user — rejected so a replayed older link
    cannot override newer state (monotonic guard, keyed by the stable per-user
    pubkey)."""
