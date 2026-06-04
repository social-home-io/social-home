"""Unit tests for the ``media_filename`` URL helper."""

from __future__ import annotations

from socialhome.routes.media_status import media_filename, video_poster_path


def test_plain_url():
    assert media_filename("api/media/abc.webm") == "abc.webm"


def test_absolute_path():
    assert media_filename("/api/media/abc.webm") == "abc.webm"


def test_signed_url_strips_query():
    assert media_filename("api/media/abc.webm?exp=1&sig=deadbeef") == "abc.webm"


def test_full_origin_url():
    assert media_filename("https://h.example/api/media/abc.webm") == "abc.webm"


def test_none_returns_none():
    assert media_filename(None) is None


def test_empty_returns_none():
    assert media_filename("") is None


def test_trailing_slash_returns_none():
    # A URL ending in ``/`` has no filename segment.
    assert media_filename("api/media/") is None


def test_bare_filename():
    assert media_filename("abc.webm") == "abc.webm"


# ─── video_poster_path ────────────────────────────────────────────────────


def test_poster_swaps_webm_for_webp():
    assert video_poster_path("api/media/abc.webm") == "api/media/abc.webp"


def test_poster_keeps_absolute_path():
    assert video_poster_path("/api/media/abc.webm") == "/api/media/abc.webp"


def test_poster_drops_signature_before_swap():
    # The signed ``.webm`` URL must yield the *unsigned* sibling ``.webp``
    # path — the caller re-signs the poster path itself.
    assert (
        video_poster_path("api/media/abc.webm?exp=1&sig=deadbeef")
        == "api/media/abc.webp"
    )


def test_poster_shares_stem_with_media():
    # Poster + media differ only in extension (shared UUID stem).
    media = "api/media/deadbeef.webm"
    poster = video_poster_path(media)
    assert poster == "api/media/deadbeef.webp"
    assert poster[: -len(".webp")] == media[: -len(".webm")]


def test_poster_none_for_non_webm():
    assert video_poster_path("api/media/abc.jpg") is None
    assert video_poster_path("api/media/abc.webp") is None


def test_poster_none_for_none():
    assert video_poster_path(None) is None


def test_poster_none_for_empty():
    assert video_poster_path("") is None
