"""Tests for the media-reference live-set query (orphan-sweep safety net)."""

import pytest

from socialhome.repositories.media_reference_repo import SqliteMediaReferenceRepo

pytestmark = pytest.mark.asyncio


async def test_empty_db_returns_empty_set(db):
    """The query must run against every source table without error."""
    repo = SqliteMediaReferenceRepo(db)
    assert await repo.referenced_basenames() == set()


async def test_collects_basenames_from_every_source(db):
    # Parents to satisfy FKs.
    await db.enqueue(
        "INSERT INTO users(user_id, display_name) VALUES('u1', 'U1')",
    )
    await db.enqueue("INSERT INTO conversations(id, type) VALUES('c1', 'dm')")
    await db.enqueue("INSERT INTO gallery_albums(id, name) VALUES('al1', 'A')")
    await db.enqueue(
        "INSERT INTO highlights(id, author_user_id, highlight_date, expires_at) "
        "VALUES('h1', 'u1', '2026-01-01', '2099-01-01T00:00:00+00:00')",
    )

    # One media-bearing row per source (URLs in the stored api/media/ form).
    await db.enqueue(
        "INSERT INTO conversation_messages(id, conversation_id, sender_user_id, "
        "media_url) VALUES('m1', 'c1', 'u1', 'api/media/dm.webp')",
    )
    await db.enqueue(
        "INSERT INTO feed_posts(id, author, type, media_url) "
        "VALUES('fp1', 'u1', 'image', '/api/media/feedsingle.webp?v=2')",
    )
    await db.enqueue(
        "INSERT INTO feed_posts(id, author, type, image_urls_json) "
        "VALUES('fp2', 'u1', 'image', "
        '\'["api/media/feed1.webp", "api/media/feed2.webp"]\')',
    )
    await db.enqueue(
        "INSERT INTO gallery_items(id, album_id, uploaded_by, item_type, "
        "filename, thumbnail_filename, width, height) "
        "VALUES('gi1', 'al1', 'u1', 'photo', 'gal.webp', 'galt.webp', 1, 1)",
    )
    await db.enqueue(
        "INSERT INTO highlight_frames(id, highlight_id, sequence, frame_type, "
        "media_url) VALUES('hf1', 'h1', 1, 'image', 'api/media/hl.webp')",
    )
    await db.enqueue(
        "INSERT INTO moments(id, author_user_id, origin_instance_id, expires_at, "
        "media_url) VALUES('mo1', 'u1', 'self', '2099-01-01T00:00:00+00:00', "
        "'api/media/mom.webp')",
    )

    repo = SqliteMediaReferenceRepo(db)
    names = await repo.referenced_basenames()
    assert names == {
        "dm.webp",
        "feedsingle.webp",  # leading slash + ?query stripped
        "feed1.webp",
        "feed2.webp",
        "gal.webp",
        "galt.webp",
        "hl.webp",
        "mom.webp",
    }
