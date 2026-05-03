"""Background scheduler that prunes expired / over-quota stories.

Two passes per tick:

1. Drop stories whose ``expires_at`` lies in the past — the cutoff is
   set per-author by their ``preferences_json["stories"].retention_days``
   when the story is first created.
2. For each author with stories, count rows and drop the oldest beyond
   their ``preferences_json["stories"].max_count`` ceiling — even if
   they have not yet expired.

Cascades through the FK chain delete the frames, views, and reactions
attached to a removed story. Share-cards in the household feed have
``ON DELETE SET NULL`` on ``feed_posts.linked_story_id`` so they degrade
to a "Story has ended" placeholder rather than disappearing.

Lifecycle follows the project-wide ``_stop: asyncio.Event`` template
(see ``replay_cache_scheduler.py``).
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..services.story_service import StoryService

log = logging.getLogger(__name__)


class StoryRetentionScheduler:
    """Periodically run :meth:`StoryService.expire_due`."""

    __slots__ = ("_service", "_interval", "_task", "_stop")

    def __init__(
        self,
        service: "StoryService",
        *,
        interval_seconds: float = 3600.0,  # one pass per hour
    ) -> None:
        self._service = service
        self._interval = interval_seconds
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    async def start(self) -> None:
        """Start the loop. Idempotent — repeated calls are no-ops."""
        if self._task is not None and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        """Set the stop event and wait briefly for the loop to exit."""
        self._stop.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._task.cancel()
            self._task = None

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                expired, over_max = await self._service.expire_due()
                if expired or over_max:
                    log.debug(
                        "story-retention: pruned %d expired, %d over-max",
                        expired,
                        over_max,
                    )
            except Exception as exc:  # pragma: no cover — defensive
                log.warning("story-retention prune failed: %s", exc)
            try:
                await asyncio.wait_for(
                    self._stop.wait(),
                    timeout=self._interval,
                )
            except asyncio.TimeoutError:
                continue


__all__ = ["StoryRetentionScheduler"]
