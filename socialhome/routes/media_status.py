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


#: Transcoded-video extension and its sibling poster extension. The
#: upload path mints both from one UUID stem (``<stem>.webm`` +
#: ``<stem>.webp``) so the poster path is derivable from the media URL.
_VIDEO_EXT = ".webm"
_POSTER_EXT = ".webp"


def video_poster_path(media_url: str | None) -> str | None:
    """Unsigned poster path for a transcoded video.

    The async video upload mints the ``.webm`` output and its ``.webp``
    poster from a single UUID stem, so the poster is the sibling
    ``.webp`` of the media URL. Drops any ``?exp=&sig=`` signature
    before swapping the extension — the caller re-signs the returned
    path. Returns ``None`` for a missing URL or any non-``.webm`` media
    (images, audio, files, federated externals).
    """
    if not media_url:
        return None
    base = media_url.split("?", 1)[0]  # drop any signature query
    if not base.endswith(_VIDEO_EXT):
        return None
    return base[: -len(_VIDEO_EXT)] + _POSTER_EXT
