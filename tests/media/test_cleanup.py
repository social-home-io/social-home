"""Tests for the fail-soft media-file unlink helper."""

import pathlib

import pytest

from socialhome.media.cleanup import media_basename, unlink_media


def test_media_basename_strips_prefix_and_query():
    assert media_basename("api/media/a.webp") == "a.webp"
    assert media_basename("/api/media/a.webp?v=3") == "a.webp"
    assert media_basename("bare.webp") == "bare.webp"


def test_media_basename_rejects_empty_and_traversal():
    assert media_basename(None) is None
    assert media_basename("") is None
    assert media_basename("api/media/..") is None


pytestmark = pytest.mark.asyncio


async def _touch(d: pathlib.Path, name: str) -> pathlib.Path:
    p = d / name
    p.write_bytes(b"x")
    return p


async def test_removes_existing_file(tmp_path):
    p = await _touch(tmp_path, "abc.webp")
    assert await unlink_media(tmp_path, "api/media/abc.webp") is True
    assert not p.exists()


async def test_tolerates_leading_slash_and_query(tmp_path):
    await _touch(tmp_path, "abc.webp")
    assert await unlink_media(tmp_path, "/api/media/abc.webp?v=2") is True
    assert not (tmp_path / "abc.webp").exists()


async def test_missing_file_is_noop(tmp_path):
    assert await unlink_media(tmp_path, "api/media/gone.webp") is False


async def test_none_and_empty_url(tmp_path):
    assert await unlink_media(tmp_path, None) is False
    assert await unlink_media(tmp_path, "") is False


async def test_no_path_traversal(tmp_path):
    # A file outside media_dir must never be reached — only the basename
    # is used, so this resolves to media_dir/passwd (which doesn't exist).
    outside = tmp_path.parent / "secret.txt"
    outside.write_bytes(b"keep me")
    assert await unlink_media(tmp_path, "api/media/../../secret.txt") is False
    assert outside.exists()
