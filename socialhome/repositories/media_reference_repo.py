"""Read the set of media filenames still referenced by any DB row.

The media orphan sweep (``MediaOrphanSweepService``) deletes files in the
media dir that nothing references. The *only* safe way to do that is to
know, completely, which filenames are still live — a single missed source
here means the sweep deletes a user's photo. This repo owns that
enumeration as one auditable place.

Like ``backup_service`` / ``data_export_service``, this is a deliberate
whole-DB read that spans tables, so it uses raw SQL directly rather than
going through per-domain repos.

Sources (verified against the schema):
  * ``conversation_messages.media_url``  — DM media (final blob)
  * ``feed_posts.media_url`` + ``image_urls_json``
  * ``space_posts.media_url`` + ``image_urls_json``
  * ``gallery_items.filename`` + ``thumbnail_filename`` (already basenames)
  * ``highlight_frames.media_url``
  * ``moments.media_url``

NOT included (owned elsewhere / transient): DM ``.preview.webp`` /
``.part<NNNN>`` / ``.assembled`` intermediates and the ``.partial/``
staging dir — the sweep skips those by pattern; ``dm_gc`` owns them.
"""

from __future__ import annotations

import json
import logging
from typing import Protocol, runtime_checkable

from ..db import AsyncDatabase
from ..media.cleanup import media_basename
from .base import rows_to_dicts

log = logging.getLogger(__name__)


@runtime_checkable
class AbstractMediaReferenceRepo(Protocol):
    async def referenced_basenames(self) -> set[str]: ...


class SqliteMediaReferenceRepo:
    """Collects every still-referenced media basename across the schema."""

    __slots__ = ("_db",)

    def __init__(self, db: AsyncDatabase) -> None:
        self._db = db

    async def referenced_basenames(self) -> set[str]:
        out: set[str] = set()

        # ── Plain media_url columns ───────────────────────────────────────
        for table in (
            "conversation_messages",
            "feed_posts",
            "space_posts",
            "highlight_frames",
            "moments",
        ):
            rows = await self._db.fetchall(
                f"SELECT media_url FROM {table} WHERE media_url IS NOT NULL",
            )
            for r in rows_to_dicts(rows):
                name = media_basename(r.get("media_url"))
                if name:
                    out.add(name)

        # ── Multi-image posts (JSON array of URLs) ────────────────────────
        for table in ("feed_posts", "space_posts"):
            rows = await self._db.fetchall(
                f"SELECT image_urls_json FROM {table} "
                f"WHERE image_urls_json IS NOT NULL",
            )
            for r in rows_to_dicts(rows):
                raw = r.get("image_urls_json")
                if not raw:
                    continue
                try:
                    urls = json.loads(raw)
                except ValueError, TypeError:  # pragma: no cover — defensive
                    continue
                if isinstance(urls, list):
                    for u in urls:
                        name = media_basename(u if isinstance(u, str) else None)
                        if name:
                            out.add(name)

        # ── Gallery (filename / thumbnail_filename are already basenames) ──
        rows = await self._db.fetchall(
            "SELECT filename, thumbnail_filename FROM gallery_items",
        )
        for r in rows_to_dicts(rows):
            for col in ("filename", "thumbnail_filename"):
                # Stored as bare filenames, but run through media_basename
                # anyway so a stray ``api/media/`` prefix can't slip past.
                name = media_basename(r.get(col))
                if name:
                    out.add(name)

        return out
