"""Regression: ``list_due`` must compare timestamps after normalising
both sides through SQLite's ``datetime()`` function.

The default for the ``next_attempt_at`` column is ``datetime('now')``
(``YYYY-MM-DD HH:MM:SS``) — so a freshly-enqueued row compares cleanly
against ``datetime('now')`` even with a raw lexical compare. But once
the processor reschedules a failed delivery, it writes a Python
``datetime.isoformat()`` value (``YYYY-MM-DDTHH:MM:SS.ffffff+00:00``).
That ``T`` lexically sorts *after* the space-separated form, so a raw
``next_attempt_at <= datetime('now')`` predicate would silently drop
every rescheduled row from the due list and the outbox would stop
retrying after the first failure.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from socialhome.domain.federation import FederationEventType
from socialhome.repositories.outbox_repo import SqliteOutboxRepo


@pytest.fixture
async def env(tmp_dir):
    from socialhome.crypto import derive_instance_id, generate_identity_keypair
    from socialhome.db.database import AsyncDatabase

    kp = generate_identity_keypair()
    iid = derive_instance_id(kp.public_key)
    db = AsyncDatabase(tmp_dir / "test.db", batch_timeout_ms=10)
    await db.startup()
    await db.enqueue(
        "INSERT INTO instance_identity(instance_id, identity_private_key,"
        " identity_public_key, routing_secret) VALUES(?,?,?,?)",
        (iid, kp.private_key.hex(), kp.public_key.hex(), "aa" * 32),
    )

    class Env:
        pass

    e = Env()
    e.db = db
    e.outbox_repo = SqliteOutboxRepo(db)
    yield e
    await db.shutdown()


async def test_list_due_picks_up_rescheduled_iso_timestamp(env):
    """A rescheduled entry whose ``next_attempt_at`` is in the past
    must come back from :meth:`list_due` even when stored as ISO-8601
    with ``T`` and a ``+00:00`` suffix.
    """
    entry_id = await env.outbox_repo.enqueue(
        instance_id="peer",
        event_type=FederationEventType.SPACE_POST_CREATED,
        payload_json="{}",
    )
    past = (
        datetime.now(timezone.utc) - timedelta(minutes=5)
    ).isoformat()  # e.g. '2026-05-09T19:30:00.000000+00:00'
    assert "T" in past and past.endswith("+00:00")

    await env.outbox_repo.reschedule(entry_id, past, attempts=1)

    due = await env.outbox_repo.list_due()
    ids = [e.id for e in due]
    assert entry_id in ids


async def test_list_due_excludes_future_iso_timestamp(env):
    """Symmetric: entries whose ISO-8601 ``next_attempt_at`` is in the
    future must NOT appear in the due list. Guards against an
    over-eager comparison that would defeat backoff.
    """
    entry_id = await env.outbox_repo.enqueue(
        instance_id="peer",
        event_type=FederationEventType.SPACE_POST_CREATED,
        payload_json="{}",
    )
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    await env.outbox_repo.reschedule(entry_id, future, attempts=1)

    due = await env.outbox_repo.list_due()
    assert all(e.id != entry_id for e in due)
