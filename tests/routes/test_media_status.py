"""Unit tests for the ``media_filename`` URL helper."""

from __future__ import annotations

from socialhome.routes.media_status import media_filename


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
