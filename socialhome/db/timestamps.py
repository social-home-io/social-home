"""Timestamp helpers — single source of truth for "now" strings the
SPA renders.

Every row that carries a timestamp eventually reaches the browser
via the SPA's ``relativeDocsTime`` / ``relativeChatTime`` helpers,
which compute "{N}h ago" by diffing the value against ``Date.now()``.
That diff is only meaningful if the string is unambiguously UTC.

The historical pattern — SQLite's ``datetime('now')`` baked into the
SQL — emits the naive shape ``"2026-05-13 18:34:56"``: UTC value,
but no ``T`` and no ``Z``. Browsers' ``Date.parse`` treats that as
the viewer's **local** time, so a fresh write reads as
"{tz-offset}h ago" everywhere east of UTC. The Highlights "Seen by"
sheet was the user-facing surface that surfaced this bug.

Two interchangeable replacements, depending on where the value is
produced:

* :data:`SQL_UTC_NOW` — drop-in for ``datetime('now')`` **inside
  SQL strings**. Renders ``strftime('%Y-%m-%dT%H:%M:%fZ', 'now')``,
  which produces ``"2026-05-13T18:34:56.123Z"`` directly from SQLite
  with no extra parameter binding.
* :func:`utc_now_iso` — Python helper for when the value is already
  being computed in Python and passed as a parameter. Returns
  ``datetime.now(timezone.utc).isoformat()`` —
  ``"2026-05-13T18:34:56.123456+00:00"``.

Both shapes parse to the same instant in any JS engine. Pick the
one that matches the surrounding code: keep SQL terse with
:data:`SQL_UTC_NOW`, keep Python explicit with :func:`utc_now_iso`.

The SPA also carries a belt-and-braces normaliser in
``client/src/utils/relativeTime.ts`` so legacy rows already on disk
still render correctly — but every new write should use one of the
two helpers here so the SPA never has to compensate.
"""

from __future__ import annotations

from datetime import datetime, timezone

# SQL fragment that yields an ISO 8601 UTC string with ``T`` separator
# and ``Z`` suffix — the shape JS's ``Date.parse`` reads unambiguously.
# Drop in wherever ``datetime('now')`` was hard-coded inside an SQL
# string. Example:
#
#     await self._db.enqueue(
#         f"INSERT INTO foo(..., created_at) VALUES(..., {SQL_UTC_NOW})",
#         (...,),
#     )
SQL_UTC_NOW = "strftime('%Y-%m-%dT%H:%M:%fZ', 'now')"


def utc_now_iso() -> str:
    """Return ``datetime.now(timezone.utc).isoformat()``.

    The trailing ``+00:00`` offset is what makes this string
    unambiguous to JavaScript's ``Date.parse`` — without it the
    browser falls back to local-time parsing and timestamps drift by
    the viewer's UTC offset.
    """
    return datetime.now(timezone.utc).isoformat()
