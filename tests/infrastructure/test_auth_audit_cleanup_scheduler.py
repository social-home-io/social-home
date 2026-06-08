"""Tests for the auth-audit cleanup scheduler."""

from __future__ import annotations

import asyncio

from socialhome.infrastructure.auth_audit_cleanup_scheduler import (
    AuthAuditCleanupScheduler,
)


class _StubRepo:
    """Records calls to ``prune_older_than`` and returns a fixed count."""

    def __init__(self, deleted: int = 7) -> None:
        self.calls: list[int] = []
        self._deleted = deleted

    async def prune_older_than(self, *, days: int = 90) -> int:
        self.calls.append(days)
        return self._deleted


async def test_prune_once_delegates_to_repo_and_returns_count():
    repo = _StubRepo(deleted=7)
    sched = AuthAuditCleanupScheduler(repo)
    pruned = await sched._prune_once()
    assert pruned == 7
    assert repo.calls == [90]


async def test_start_stop_is_clean():
    """``start()`` is idempotent and ``stop()`` exits the task."""
    repo = _StubRepo(deleted=0)
    sched = AuthAuditCleanupScheduler(repo, interval_seconds=0.05)
    await sched.start()
    await sched.start()  # idempotent — second start is a noop
    await asyncio.sleep(0.1)
    await sched.stop()
    # Re-stop is fine.
    await sched.stop()
    assert repo.calls  # the loop ran at least once
