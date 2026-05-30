"""Tests for the media orphan sweep — the safety-critical 'keep' rules."""

import os
import time

import pytest

from socialhome.services.media_orphan_sweep_service import (
    GRACE_SECONDS,
    MediaOrphanSweepService,
)

pytestmark = pytest.mark.asyncio


class _FakeRefRepo:
    def __init__(self, names):
        self._names = set(names)

    async def referenced_basenames(self):
        return set(self._names)


def _write(dir_, name, *, age_seconds=0):
    p = dir_ / name
    p.write_bytes(b"x")
    if age_seconds:
        old = time.time() - age_seconds
        os.utime(p, (old, old))
    return p


async def test_deletes_only_old_unreferenced_files(tmp_path):
    referenced = _write(tmp_path, "keep-me.webp", age_seconds=2 * GRACE_SECONDS)
    fresh_orphan = _write(tmp_path, "fresh.webp")  # mtime ~now
    old_orphan = _write(tmp_path, "old.webp", age_seconds=2 * GRACE_SECONDS)
    # DM intermediates (owned by dm_gc) — old + unreferenced but skip-patterned.
    preview = _write(tmp_path, "abc123.preview.webp", age_seconds=2 * GRACE_SECONDS)
    part = _write(tmp_path, "abc123.part00001", age_seconds=2 * GRACE_SECONDS)
    assembled = _write(tmp_path, "abc123.assembled.webp", age_seconds=2 * GRACE_SECONDS)
    # A staging subdirectory must never be touched.
    partial = tmp_path / ".partial"
    partial.mkdir()
    (partial / "chunk").write_bytes(b"y")

    svc = MediaOrphanSweepService(
        media_dir=tmp_path,
        reference_repo=_FakeRefRepo({"keep-me.webp"}),
    )
    removed = await svc.sweep_once()

    assert removed == 1
    assert not old_orphan.exists()  # the only thing deleted
    assert referenced.exists()  # referenced → kept
    assert fresh_orphan.exists()  # within grace → kept
    assert preview.exists() and part.exists() and assembled.exists()  # skip-pattern
    assert (partial / "chunk").exists()  # subdir skipped


async def test_missing_media_dir_is_noop(tmp_path):
    svc = MediaOrphanSweepService(
        media_dir=tmp_path / "does-not-exist",
        reference_repo=_FakeRefRepo(set()),
    )
    assert await svc.sweep_once() == 0


async def test_empty_reference_set_still_respects_grace(tmp_path):
    fresh = _write(tmp_path, "fresh.webp")
    old = _write(tmp_path, "old.webp", age_seconds=2 * GRACE_SECONDS)
    svc = MediaOrphanSweepService(
        media_dir=tmp_path, reference_repo=_FakeRefRepo(set())
    )
    removed = await svc.sweep_once()
    assert removed == 1
    assert fresh.exists()
    assert not old.exists()
