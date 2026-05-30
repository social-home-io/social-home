"""Fail-soft removal of a stored media file given its ``api/media/`` URL.

Several services own a media file 1:1 with a DB row (a DM message blob,
a gallery upload, …) and must delete the file when the row goes away —
otherwise blobs accumulate on disk forever. They all need the same
small, defensive operation: map the stored ``api/media/<filename>`` URL
to ``media_dir/<basename>`` and remove it without ever letting disk
state block the row deletion.

This helper is deliberately conservative — it only resolves the URL's
*basename* under ``media_dir`` (no path traversal) and swallows a
missing file / FS error. Callers that share a blob across rows (e.g.
feed-post media mirrored into a gallery system album) must NOT use this
on a per-row delete — see the media-cleanup notes; this is for
1:1-owned files only.
"""

from __future__ import annotations

import logging
import pathlib

import aiofiles.os

log = logging.getLogger(__name__)


def media_basename(media_url: str | None) -> str | None:
    """Resolve a stored ``api/media/<file>`` URL to its bare filename.

    Tolerates a leading ``/`` and a ``?query``. Returns ``None`` for an
    empty/``.``/``..`` value. Splitting on ``/`` guarantees the result has
    no path separator, so ``media_dir / basename`` can't escape media_dir.
    """
    if not media_url:
        return None
    name = media_url.rsplit("/", 1)[-1].split("?", 1)[0]
    if not name or name in (".", ".."):
        return None
    return name


async def unlink_media(media_dir: pathlib.Path, media_url: str | None) -> bool:
    """Best-effort delete of the local file backing ``media_url``.

    ``media_url`` is the stored ``api/media/<filename>`` form (a leading
    ``/`` and a ``?query`` are tolerated). Returns ``True`` iff a file
    was removed. Never raises: a missing file, an unresolvable URL, or a
    filesystem error is logged at debug and swallowed.
    """
    filename = media_basename(media_url)
    if filename is None:
        return False
    path = media_dir / filename
    try:
        await aiofiles.os.remove(path)
        return True
    except FileNotFoundError:
        return False
    except OSError as exc:  # pragma: no cover — defensive
        log.debug("unlink_media: failed to remove %s: %s", path, exc)
        return False
