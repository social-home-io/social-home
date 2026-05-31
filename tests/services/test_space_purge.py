"""Tests for the shared space hard-delete helper (``space_purge``)."""

from __future__ import annotations

import pathlib

import pytest

from socialhome.services.space_purge import (
    collect_space_media_urls,
    purge_space_and_media,
)

pytestmark = pytest.mark.asyncio


class _FakePostRepo:
    def __init__(self, urls: list[str]) -> None:
        self._urls = urls

    async def list_space_media_urls(self, space_id: str) -> list[str]:
        return list(self._urls)


class _FakeGalleryRepo:
    def __init__(self, names: list[str]) -> None:
        self._names = names

    async def list_space_item_filenames(self, space_id: str) -> list[str]:
        return list(self._names)


class _FakeSpaceRepo:
    def __init__(self) -> None:
        self.purged: list[str] = []

    async def purge(self, space_id: str) -> None:
        self.purged.append(space_id)


async def test_collect_dedupes_exact_duplicates_order_preserving():
    # Exact-string dedup (e.g. a post listed twice); the differing
    # post-URL vs bare-basename forms are NOT collapsed, which is fine —
    # unlinking the same basename twice is an idempotent no-op.
    posts = _FakePostRepo(["api/media/a.webp", "api/media/b.webp", "api/media/a.webp"])
    gallery = _FakeGalleryRepo(["g1.webp", "g2.webp", "g1.webp"])
    urls = await collect_space_media_urls(
        post_repo=posts, gallery_repo=gallery, space_id="sp-1"
    )
    assert urls == ["api/media/a.webp", "api/media/b.webp", "g1.webp", "g2.webp"]


async def test_collect_without_gallery_repo():
    posts = _FakePostRepo(["api/media/x.webp"])
    urls = await collect_space_media_urls(
        post_repo=posts, gallery_repo=None, space_id="sp-1"
    )
    assert urls == ["api/media/x.webp"]


async def test_purge_drops_rows_and_unlinks_files(tmp_path: pathlib.Path):
    # Two real files; one referenced by a post, one by the gallery.
    (tmp_path / "post.webp").write_bytes(b"P")
    (tmp_path / "gallery.webp").write_bytes(b"G")
    (tmp_path / "unrelated.webp").write_bytes(b"U")
    space_repo = _FakeSpaceRepo()
    removed = await purge_space_and_media(
        space_repo=space_repo,
        post_repo=_FakePostRepo(["api/media/post.webp"]),
        gallery_repo=_FakeGalleryRepo(["gallery.webp"]),
        media_dir=tmp_path,
        space_id="sp-1",
    )
    assert space_repo.purged == ["sp-1"]  # rows dropped (cascade)
    assert removed == 2
    assert not (tmp_path / "post.webp").exists()
    assert not (tmp_path / "gallery.webp").exists()
    assert (tmp_path / "unrelated.webp").exists()  # untouched


async def test_purge_without_media_dir_still_drops_rows():
    space_repo = _FakeSpaceRepo()
    removed = await purge_space_and_media(
        space_repo=space_repo,
        post_repo=_FakePostRepo(["api/media/x.webp"]),
        gallery_repo=None,
        media_dir=None,
        space_id="sp-9",
    )
    assert space_repo.purged == ["sp-9"]
    assert removed == 0


async def test_purge_tolerates_missing_files(tmp_path: pathlib.Path):
    space_repo = _FakeSpaceRepo()
    removed = await purge_space_and_media(
        space_repo=space_repo,
        post_repo=_FakePostRepo(["api/media/gone.webp"]),
        gallery_repo=None,
        media_dir=tmp_path,
        space_id="sp-2",
    )
    assert space_repo.purged == ["sp-2"]
    assert removed == 0  # missing file → no-op, no raise
