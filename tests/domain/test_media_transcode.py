"""The :class:`MediaTranscodeJob` row dataclass is a frozen DTO."""

from __future__ import annotations

import dataclasses

import pytest

from socialhome.domain.media_transcode import MediaTranscodeJob


def test_media_transcode_job_is_frozen():
    job = MediaTranscodeJob(
        output_filename="out.webm",
        source_path="/tmp/src",
        thumbnail_filename="poster.webp",
        kind="video",
        owner_user_id="u-1",
        status="pending",
        attempts=0,
        next_attempt_at="2026-06-04 00:00:00",
        last_error=None,
        created_at="2026-06-04 00:00:00",
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        job.status = "processing"  # type: ignore[misc]
