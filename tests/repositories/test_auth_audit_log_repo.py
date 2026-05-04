"""Unit tests for the auth audit log repo."""

from __future__ import annotations

import pytest

from socialhome.db import AsyncDatabase
from socialhome.repositories.auth_audit_log_repo import (
    SqliteAuthAuditLogRepo,
)


@pytest.fixture
async def db(tmp_path):
    sqlite = tmp_path / "test.db"
    db = AsyncDatabase(str(sqlite))
    await db.startup()
    yield db
    await db.shutdown()


async def test_record_then_list_returns_row(db):
    repo = SqliteAuthAuditLogRepo(db)
    await repo.record(
        "login_success",
        username="alice",
        ip_address="127.0.0.1",
    )
    rows = await repo.list_recent()
    assert len(rows) == 1
    assert rows[0]["event_type"] == "login_success"
    assert rows[0]["username"] == "alice"
    assert rows[0]["ip_address"] == "127.0.0.1"


async def test_metadata_round_trips_as_dict(db):
    repo = SqliteAuthAuditLogRepo(db)
    await repo.record(
        "reset_issue",
        username="bob",
        metadata={"issued_by": "admin", "ttl": 3600},
    )
    rows = await repo.list_recent()
    assert rows[0]["metadata"] == {"issued_by": "admin", "ttl": 3600}


async def test_username_optional(db):
    """``login_failure`` for an unknown user has no recoverable principal."""
    repo = SqliteAuthAuditLogRepo(db)
    await repo.record(
        "login_failure",
        ip_address="10.0.0.1",
    )
    rows = await repo.list_recent()
    assert rows[0]["username"] is None


async def test_list_recent_orders_newest_first(db):
    repo = SqliteAuthAuditLogRepo(db)
    await repo.record("login_success", username="a")
    await repo.record("login_success", username="b")
    await repo.record("login_success", username="c")
    rows = await repo.list_recent()
    # Newest first.
    assert [r["username"] for r in rows] == ["c", "b", "a"]


async def test_list_recent_honours_limit(db):
    repo = SqliteAuthAuditLogRepo(db)
    for _ in range(5):
        await repo.record("login_success", username="alice")
    rows = await repo.list_recent(limit=3)
    assert len(rows) == 3
