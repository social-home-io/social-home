"""Protocol-level media constraints (§5.2).

These values are **not** operator-configurable — they are part of the Social
Home wire protocol.  Every instance in the federation must agree on the same
limits so that media exchanged between instances is always accepted.

Local upload processing (image_processor, video_processor) uses these
constants to produce conformant output.  Inbound federation validation
(media_validator) uses them to reject non-conforming payloads.
"""

from __future__ import annotations


# ─── Image constraints ──────────────────────────────────────────────────────

#: Longest-side cap for uploaded photos / post images. 2560 px is a
#: 1:1 fit for the native short edge of an iPad Pro 12.9 (2732×2048)
#: and a 14"/16" retina MacBook (1964/2234 short edge), so a photo
#: viewed full-bleed on those screens is no longer software-stretched.
#: The previous 2048 px cap pre-dated retina laptops as a baseline.
IMAGE_MAX_DIMENSION: int = 2560
#: WebP quality factor. 82 is the threshold where the mild Q78
#: artifacts (skin-tone blockiness, gradient banding on skies) clean
#: up visibly on typical household photos. ~+18% file size over 78
#: per image. Thumbnails stay at Q75 — at 512 px the perceived
#: difference is dominated by display scaling, so the savings are
#: free.
IMAGE_WEBP_QUALITY: int = 82
IMAGE_MAX_UPLOAD_BYTES: int = 20 * 1024 * 1024  # 20 MiB
IMAGE_ACCEPTED_MIMES: frozenset[str] = frozenset(
    {
        "image/jpeg",
        "image/png",
        "image/gif",
        "image/webp",
        "image/heic",
    }
)
IMAGE_OUTPUT_MIME: str = "image/webp"
IMAGE_WEBP_MAGIC: bytes = b"RIFF"
IMAGE_WEBP_MAGIC_8: bytes = b"WEBP"

# ─── Video constraints ──────────────────────────────────────────────────────

#: Longest-side cap. 1920 px is true 1080p — matches what every flagship
#: phone captures by default and the playback target (laptop / TV) every
#: viewer is likely on. The previous 1280 px (720p) read as low quality
#: on anything bigger than a phone screen.
VIDEO_MAX_DIMENSION: int = 1920
#: x264 CRF — log-scale quality knob (-6 ≈ 2× file size). 25 sits in
#: the "visually transparent for normal household content" band; the
#: previous 28 left visible smearing on motion and banding on
#: gradients. Cost: ~1.4× per-clip file size at the same resolution,
#: ~2.8× combined with the 720p → 1080p bump.
VIDEO_CRF: int = 25
VIDEO_MAX_DURATION_SECONDS: int = 60
VIDEO_AUDIO_BITRATE_KBPS: int = 96
VIDEO_MAX_UPLOAD_BYTES: int = 200 * 1024 * 1024  # 200 MiB
VIDEO_ACCEPTED_MIMES: frozenset[str] = frozenset(
    {
        "video/mp4",
        "video/webm",
        "video/quicktime",
    }
)
VIDEO_OUTPUT_MIME: str = "video/webm"
VIDEO_WEBM_MAGIC: bytes = b"\x1a\x45\xdf\xa3"

# ─── Shared ─────────────────────────────────────────────────────────────────

#: Grid-thumbnail longest side. 512 px keeps tablet- and desktop-grid
#: thumbs sharp at 3× retina (~170 dp wide). The previous 400 px was a
#: phone-grid target and read soft on bigger screens.
THUMBNAIL_PX: int = 512
THUMBNAIL_WEBP_QUALITY: int = 75
CAPTION_MAX: int = 300

# ─── Profile / cover uploads ───────────────────────────────────────────────
#: Avatars and space cover images both ride through ImageProcessor, which
#: transcodes to WebP and caps the longest side — so the raw upload cap is
#: about accommodating high-resolution phone photos (HEIC/JPEG) rather than
#: about the on-disk size. One shared constant keeps the three upload sites
#: (profile picture, per-space member picture, space cover) in lockstep.
PROFILE_PICTURE_MAX_UPLOAD_BYTES: int = 10 * 1024 * 1024  # 10 MiB

#: Space cover image resized to this longest side; larger than the 256-px
#: profile-picture cap so a hero banner has real estate.
SPACE_COVER_MAX_DIMENSION: int = 1200
