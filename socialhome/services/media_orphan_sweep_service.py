"""Delete media-dir files that no DB row references (orphans).

The per-delete cleanup paths (DM, gallery, posts, highlights, moments) catch
the common cases, but some files still leak — most notably media whose owning
row was removed by a *remote* federation event. This sweep is the backstop:
it lists the media dir, and deletes any file that

  1. is NOT referenced by any DB row (see ``AbstractMediaReferenceRepo``), AND
  2. is older than :data:`GRACE_SECONDS` (so in-flight uploads / freshly
     created rows whose commit races the sweep are never reaped), AND
  3. is NOT a transient DM intermediate (``*.preview.webp`` / ``*.part<NNNN>``
     / ``*.assembled*``) — those aren't in any ``media_url`` column and are
     owned by ``dm_gc``; sweeping them would clobber in-progress transfers.

Only top-level regular files are considered — sub-directories (the DM
``.partial/`` staging area) are skipped entirely.

Fail-soft: any per-file error is logged and skipped so one bad file never
aborts the pass.
"""

from __future__ import annotations

import logging
import pathlib
import re
import stat
import time

import aiofiles.os

from ..repositories.media_reference_repo import AbstractMediaReferenceRepo

log = logging.getLogger(__name__)

#: Files younger than this (by mtime) are never swept — guards in-flight
#: uploads and rows whose DB commit races a sweep pass.
GRACE_SECONDS: int = 24 * 60 * 60

#: Transient DM artifacts that aren't referenced by a ``media_url`` column.
#: ``dm_gc`` owns these; the sweep must leave them alone.
_SKIP_RE = re.compile(r"(\.preview\.webp|\.part\d+|\.assembled)", re.IGNORECASE)


class MediaOrphanSweepService:
    """One-pass sweep of unreferenced media files."""

    __slots__ = ("_media_dir", "_refs", "_grace")

    def __init__(
        self,
        *,
        media_dir: pathlib.Path,
        reference_repo: AbstractMediaReferenceRepo,
        grace_seconds: int = GRACE_SECONDS,
    ) -> None:
        self._media_dir = media_dir
        self._refs = reference_repo
        self._grace = grace_seconds

    async def sweep_once(self, *, now: float | None = None) -> int:
        """Delete orphaned media files; return the count removed."""
        now = time.time() if now is None else now
        try:
            entries = await aiofiles.os.listdir(self._media_dir)
        except FileNotFoundError:
            return 0
        except OSError as exc:  # pragma: no cover — defensive
            log.warning("media-sweep: listdir failed: %s", exc)
            return 0

        referenced = await self._refs.referenced_basenames()
        removed = 0
        for name in entries:
            if name in referenced or _SKIP_RE.search(name):
                continue
            path = self._media_dir / name
            try:
                st = await aiofiles.os.stat(path)
                # Skip directories (e.g. the DM ``.partial/`` staging area)
                # and anything still within the grace window.
                if stat.S_ISDIR(st.st_mode):
                    continue
                if now - st.st_mtime < self._grace:
                    continue
                await aiofiles.os.remove(path)
                removed += 1
            except FileNotFoundError:
                continue
            except OSError as exc:  # pragma: no cover — defensive
                log.debug("media-sweep: could not remove %s: %s", path, exc)
        if removed:
            log.info("media-sweep: removed %d orphaned media file(s)", removed)
        return removed
