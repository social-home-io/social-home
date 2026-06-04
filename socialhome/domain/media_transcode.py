"""Row-shaped DTO for the async video-transcode outbox.

Pure dataclass — no I/O, no service/repo imports. Mirrors a row of
``media_transcode_jobs`` (migration ``0026``). The transcode scheduler
consumes these and turns each into a background transcode job.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True, frozen=True)
class MediaTranscodeJob:
    """One pending / in-progress / failed video transcode."""

    output_filename: str
    source_path: str
    thumbnail_filename: str
    kind: str
    owner_user_id: str | None
    status: str
    attempts: int
    next_attempt_at: str
    last_error: str | None
    created_at: str | None
