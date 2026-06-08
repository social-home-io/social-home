"""Tests for the notification cleanup scheduler."""

from __future__ import annotations

import asyncio

from socialhome.infrastructure.notification_cleanup_scheduler import (
    NotificationCleanupScheduler,
)


class _StubRepo:
    """Records calls to ``delete_old`` and returns a fixed count."""

    def __init__(self, deleted: int = 5) -> None:
        self.calls: list[int] = []
        self._deleted = deleted

    async def delete_old(self, older_than_days: int = 90) -> int:
        self.calls.append(older_than_days)
        return self._deleted


async def test_prune_once_delegates_to_repo_and_returns_count():
    repo = _StubRepo(deleted=5)
    sched = NotificationCleanupScheduler(repo)
    pruned = await sched._prune_once()
    assert pruned == 5
    assert repo.calls == [90]


async def test_start_stop_is_clean():
    """``start()`` is idempotent and ``stop()`` exits the task."""
    repo = _StubRepo(deleted=0)
    sched = NotificationCleanupScheduler(repo, interval_seconds=0.05)
    await sched.start()
    await sched.start()  # idempotent — second start is a noop
    await asyncio.sleep(0.1)
    await sched.stop()
    # Re-stop is fine.
    await sched.stop()
    assert repo.calls  # the loop ran at least once
