"""Shared helper for surfacing async-transcode readiness on list rows.

Video uploads transcode in the background (``media_transcode_jobs`` +
``AbstractMediaTranscodeRepo``). Every list endpoint that can return a
VIDEO item adds a ``media_status`` field so the SPA renders a
"Processing…" placeholder until the ``.webm`` exists on disk.

The repo's ``status_for`` is keyed by the *output filename* — the last
path segment of the media URL, minus any short-lived ``?exp=&sig=``
signature query. :func:`media_filename` extracts that key so a handler
can batch one ``status_for`` call per request, then look each video
item's status up by filename (absent → ``"ready"``).
"""

from __future__ import annotations

#: Status reported for a video whose transcode is done (no row left).
READY = "ready"


def media_filename(url: str | None) -> str | None:
    """Last path segment of a media URL, minus any query string.

    Returns ``None`` for a missing URL or one with no filename segment
    (e.g. a trailing slash), so callers can skip it cleanly.
    """
    if not url:
        return None
    return url.split("?", 1)[0].rsplit("/", 1)[-1] or None
