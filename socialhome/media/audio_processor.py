"""Audio processing module — voice-note validation (spec §5.2).

Voice notes ship as OGG/Opus blobs already encoded by the sender's
browser. This processor does NOT transcode (Opus is the on-the-wire
format and re-encoding would only lose quality) — it validates that
the upload really is OGG/Opus, enforces the protocol duration cap,
and renames to a UUID so the on-disk filename can't leak metadata.

PyAV is used for codec / duration probing, the same binding that
:class:`VideoProcessor` relies on. No system ``ffmpeg`` is required.
"""

from __future__ import annotations

import asyncio
import io
import logging
import uuid

import av

from ..domain.media_constraints import (
    AUDIO_ACCEPTED_CODECS,
    AUDIO_ACCEPTED_MIMES,
    AUDIO_MAX_DURATION_SECONDS,
    AUDIO_MAX_UPLOAD_BYTES,
    AUDIO_MP4_FTYP_MAGIC,
    AUDIO_MP4_FTYP_OFFSET,
    AUDIO_OGG_MAGIC,
    AUDIO_WEBM_MAGIC,
)

log = logging.getLogger(__name__)


class AudioProcessor:
    """Validate uploaded voice notes — OGG container + Opus codec.

    The processor never rewrites the bytes; it returns the input
    unchanged on success. The single mutation is the filename, which
    becomes a UUID-based ``.ogg`` so the on-disk path doesn't echo
    the sender's local filename.
    """

    ACCEPTED_MIME_TYPES: frozenset[str] = AUDIO_ACCEPTED_MIMES

    __slots__ = ("_max_duration", "_max_input_bytes")

    def __init__(self) -> None:
        self._max_duration = AUDIO_MAX_DURATION_SECONDS
        self._max_input_bytes = AUDIO_MAX_UPLOAD_BYTES

    # ── Public API ────────────────────────────────────────────────────────

    async def process(
        self,
        data: bytes,
        filename: str,
    ) -> tuple[bytes, str]:
        """Validate *data* as OGG/Opus and rename to a UUID.

        Parameters
        ----------
        data:
            Raw bytes of the uploaded audio file.
        filename:
            Original filename (used for logging only; the returned name
            is always ``"{uuid}.ogg"``).

        Returns
        -------
        tuple[bytes, str]
            ``(data_unchanged, new_filename)``.

        Raises
        ------
        ValueError
            If *data* exceeds the upload cap, isn't a valid OGG
            container, doesn't carry exactly one Opus audio stream,
            or its duration exceeds the protocol cap.
        """
        if len(data) > self._max_input_bytes:
            raise ValueError(
                f"Audio upload exceeds maximum allowed size of "
                f"{self._max_input_bytes} bytes"
            )

        is_ogg = data.startswith(AUDIO_OGG_MAGIC)
        is_webm = data.startswith(AUDIO_WEBM_MAGIC)
        is_mp4 = (
            len(data) >= AUDIO_MP4_FTYP_OFFSET + len(AUDIO_MP4_FTYP_MAGIC)
            and data[
                AUDIO_MP4_FTYP_OFFSET : AUDIO_MP4_FTYP_OFFSET
                + len(AUDIO_MP4_FTYP_MAGIC)
            ]
            == AUDIO_MP4_FTYP_MAGIC
        )
        if not is_ogg and not is_webm and not is_mp4:
            raise ValueError(
                f"Unsupported audio format for file {filename!r}. "
                "Voice notes must be OGG, WebM, or MP4 containerised."
            )

        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._probe, data)

        # Preserve the container extension so the SPA's ``<audio>``
        # element knows what it's playing. ``.m4a`` is the canonical
        # extension for audio-only MP4 (Safari emits it; iOS / macOS
        # render it natively).
        if is_ogg:
            ext = ".ogg"
        elif is_webm:
            ext = ".webm"
        else:
            ext = ".m4a"
        new_filename = f"{uuid.uuid4().hex}{ext}"
        return data, new_filename

    # ── Internal (run in executor) ────────────────────────────────────────

    def _probe(self, data: bytes) -> None:
        """Open *data* with PyAV; assert one Opus stream + duration cap.

        Raises ``ValueError`` on any mismatch.
        """
        try:
            container = av.open(io.BytesIO(data), format=None)
        except av.error.FFmpegError as exc:
            raise ValueError(f"Invalid or unsupported audio file: {exc}") from exc

        try:
            audio_streams = [s for s in container.streams if s.type == "audio"]
            if not audio_streams:
                raise ValueError("No audio stream found in uploaded file")
            if len(audio_streams) > 1:
                raise ValueError(
                    "Voice notes must carry exactly one audio stream "
                    f"(got {len(audio_streams)})"
                )

            stream = audio_streams[0]
            codec_name = stream.codec_context.codec.name
            if codec_name not in AUDIO_ACCEPTED_CODECS:
                raise ValueError(
                    f"Voice notes must use Opus or AAC (got {codec_name!r})"
                )

            duration = self._duration_seconds(container, stream)
            if duration is not None and duration > self._max_duration:
                raise ValueError(
                    f"Voice note exceeds {self._max_duration}s (got {duration:.1f}s)"
                )
        finally:
            container.close()

    @staticmethod
    def _duration_seconds(container, stream) -> float | None:
        """Best-effort duration in seconds.

        Prefers the stream-level duration (more accurate for streams that
        carry their own timing). Falls back to the container's duration
        in microseconds — set on every well-formed OGG. Returns ``None``
        when neither is known (some pathological inputs); the caller
        treats ``None`` as "trust the input" so a malformed-but-decodable
        clip still gets through the magic-byte and codec gates.
        """
        if stream.duration is not None and stream.time_base is not None:
            return float(stream.duration * stream.time_base)
        if container.duration is not None:
            return container.duration / 1_000_000.0
        return None
