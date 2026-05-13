"""Tests for :mod:`socialhome.db.timestamps`.

The helpers are short, but they're load-bearing — every "{N}h ago"
label in the SPA depends on the string they emit being parseable as
UTC by ``Date.parse``. The tests pin both shapes (Python + SQL) so a
regression to the naive ``datetime('now')`` form trips here first.
"""

from __future__ import annotations

import re

import aiosqlite
import pytest

from socialhome.db.timestamps import SQL_UTC_NOW, utc_now_iso


def test_utc_now_iso_is_tz_aware_iso8601() -> None:
    s = utc_now_iso()
    # Either ``+00:00`` (Python isoformat with timezone) or ``Z``;
    # both are valid ISO 8601 UTC and parse unambiguously in JS.
    assert s.endswith("+00:00") or s.endswith("Z")
    assert "T" in s


@pytest.mark.asyncio
async def test_sql_utc_now_emits_T_separator_and_Z() -> None:
    """The SQL fragment should produce a string JS's Date.parse can
    treat as unambiguously UTC. We bind it into a SELECT and inspect
    the shape — no INSERT machinery needed."""
    async with aiosqlite.connect(":memory:") as conn:
        cur = await conn.execute(f"SELECT {SQL_UTC_NOW}")
        row = await cur.fetchone()
        assert row is not None
        ts = row[0]
        assert isinstance(ts, str)
        # ``YYYY-MM-DDTHH:MM:SS.fffZ`` — note the ``T`` and the ``Z``.
        # The naive shape would be ``YYYY-MM-DD HH:MM:SS`` (space,
        # no ``Z``) which is precisely what we never want to emit.
        assert re.match(
            r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+Z$",
            ts,
        ), f"Expected ISO-8601 UTC shape, got {ts!r}"
