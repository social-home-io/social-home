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


class _FakeTranscodeRepo:
    def __init__(self, active):
        self._active = set(active)

    async def active_source_paths(self):
        return set(self._active)


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


async def test_transcode_src_sweep_noop_without_repo(tmp_path):
    src_dir = tmp_path / "transcode_src"
    src_dir.mkdir()
    _write(src_dir, "stale.bin", age_seconds=2 * GRACE_SECONDS)
    svc = MediaOrphanSweepService(
        media_dir=tmp_path, reference_repo=_FakeRefRepo(set())
    )
    assert await svc.sweep_transcode_src_once() == 0
    assert (src_dir / "stale.bin").exists()


async def test_transcode_src_sweep_missing_dir_is_noop(tmp_path):
    svc = MediaOrphanSweepService(
        media_dir=tmp_path,
        reference_repo=_FakeRefRepo(set()),
        media_transcode_repo=_FakeTranscodeRepo(set()),
    )
    assert await svc.sweep_transcode_src_once() == 0


async def test_transcode_src_sweep_keeps_referenced_and_young(tmp_path):
    src_dir = tmp_path / "transcode_src"
    src_dir.mkdir()
    referenced = _write(src_dir, "live.bin", age_seconds=2 * GRACE_SECONDS)
    old_orphan = _write(src_dir, "leaked.bin", age_seconds=2 * GRACE_SECONDS)
    young_orphan = _write(src_dir, "fresh.bin")  # within grace

    svc = MediaOrphanSweepService(
        media_dir=tmp_path,
        reference_repo=_FakeRefRepo(set()),
        media_transcode_repo=_FakeTranscodeRepo({str(referenced)}),
    )
    removed = await svc.sweep_transcode_src_once()

    assert removed == 1
    assert referenced.exists()  # still referenced by a job row
    assert young_orphan.exists()  # within grace
    assert not old_orphan.exists()  # leaked + old → reaped


async def test_top_level_sweep_ignores_transcode_src_subdir(tmp_path):
    # The subdir itself (and an old orphan inside it) must survive the
    # top-level pass — only sweep_transcode_src_once touches that dir.
    src_dir = tmp_path / "transcode_src"
    src_dir.mkdir()
    inside = _write(src_dir, "leaked.bin", age_seconds=2 * GRACE_SECONDS)
    old_top = _write(tmp_path, "old.webp", age_seconds=2 * GRACE_SECONDS)

    svc = MediaOrphanSweepService(
        media_dir=tmp_path,
        reference_repo=_FakeRefRepo(set()),
        media_transcode_repo=_FakeTranscodeRepo(set()),
    )
    removed = await svc.sweep_once()

    # Only the top-level orphan was removed by sweep_once.
    assert removed == 1
    assert not old_top.exists()
    assert src_dir.is_dir()
    assert inside.exists()
