"""Tests for the SH-side ``/api/stories/{id}/publish*`` routes.

The routes delegate to :class:`StoryPublicationService`; here we
inject a stub publication service so the tests don't need a real
GFS round-trip. The auth + match-info wiring is what's under test.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from socialhome.app_keys import (
    story_publication_service_key,
    story_repo_key,
)
from socialhome.services.story_publication_service import (
    StoryNotFoundError,
    StoryPublicationError,
)

from .conftest import _auth


class _StubService:
    def __init__(self):
        self.published_returns = {
            "token": "tok-A",
            "url": "https://gfs.example/u/s/A",
            "label": None,
        }
        self.publish_calls: list[tuple] = []
        self.revoke_calls: list[tuple] = []
        self.unpublish_calls: list[tuple] = []
        self.publish_raises: Exception | None = None
        self.revoke_raises: Exception | None = None
        self.unpublish_raises: Exception | None = None

    async def publish(self, story_id, user_id, *, gfs_id, label=None):
        self.publish_calls.append((story_id, user_id, gfs_id, label))
        if self.publish_raises:
            raise self.publish_raises
        return {**self.published_returns, "label": label}

    async def revoke_token(self, story_id, user_id, *, token):
        self.revoke_calls.append((story_id, user_id, token))
        if self.revoke_raises:
            raise self.revoke_raises

    async def unpublish(self, story_id, user_id):
        self.unpublish_calls.append((story_id, user_id))
        if self.unpublish_raises:
            raise self.unpublish_raises


def _expires(days: int = 7) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()


@pytest.fixture
async def seeded_story(client):
    """Create a real story row owned by the test user."""
    story_id = "s-1"
    await client._db.enqueue(
        """
        INSERT INTO stories(
            id, author_user_id, story_date, audience_kind, audience_json,
            created_at, expires_at
        ) VALUES(?,?,?,?,?, datetime('now'), ?)
        """,
        (story_id, client._uid, "2026-05-03", "all_paired", "[]", _expires()),
    )
    return story_id


@pytest.fixture
def stub_service(client):
    stub = _StubService()
    client.app[story_publication_service_key] = stub
    return stub


# ── POST publish ────────────────────────────────────────────────────────


async def test_publish_returns_url_and_token(client, seeded_story, stub_service):
    r = await client.post(
        f"/api/stories/{seeded_story}/publish",
        json={"gfs_id": "gfs-abc", "label": "twitter"},
        headers=_auth(client._tok),
    )
    assert r.status == 201
    body = await r.json()
    assert body["token"] == "tok-A"
    assert body["url"].startswith("https://gfs.example/")
    assert body["label"] == "twitter"
    assert stub_service.publish_calls == [
        (seeded_story, client._uid, "gfs-abc", "twitter"),
    ]


async def test_publish_without_gfs_id_returns_422(client, seeded_story, stub_service):
    r = await client.post(
        f"/api/stories/{seeded_story}/publish",
        json={"label": "twitter"},
        headers=_auth(client._tok),
    )
    assert r.status == 422
    assert stub_service.publish_calls == []


async def test_publish_unauthenticated_returns_401(client, seeded_story):
    r = await client.post(
        f"/api/stories/{seeded_story}/publish",
        json={"gfs_id": "gfs-abc"},
    )
    assert r.status == 401


async def test_publish_other_users_story_returns_404(
    client,
    seeded_story,
    stub_service,
):
    stub_service.publish_raises = StoryNotFoundError(seeded_story)
    r = await client.post(
        f"/api/stories/{seeded_story}/publish",
        json={"gfs_id": "gfs-abc"},
        headers=_auth(client._tok),
    )
    assert r.status == 404


async def test_publish_gfs_failure_maps_to_502(
    client,
    seeded_story,
    stub_service,
):
    stub_service.publish_raises = StoryPublicationError("GFS down")
    r = await client.post(
        f"/api/stories/{seeded_story}/publish",
        json={"gfs_id": "gfs-abc"},
        headers=_auth(client._tok),
    )
    assert r.status == 502
    body = await r.json()
    assert body["error"]["code"] == "GFS_UNAVAILABLE"


# ── GET publish ─────────────────────────────────────────────────────────


async def test_get_publish_state_when_published(client, seeded_story):
    await client._db.enqueue(
        """
        INSERT INTO gfs_connections(
            id, gfs_instance_id, display_name, public_key, inbox_url,
            status, paired_at
        ) VALUES('gfs-abc','gfs-abc','GFS','ff','https://gfs',
                 'active', datetime('now'))
        """,
    )
    repo = client.app[story_repo_key]
    await repo.mark_published(
        seeded_story,
        gfs_id="gfs-abc",
        published_at=datetime.now(timezone.utc).isoformat(),
    )
    r = await client.get(
        f"/api/stories/{seeded_story}/publish",
        headers=_auth(client._tok),
    )
    assert r.status == 200
    body = await r.json()
    assert body["published"] is True
    assert body["gfs_id"] == "gfs-abc"
    assert body["published_at"]


async def test_get_publish_state_when_unpublished(client, seeded_story):
    r = await client.get(
        f"/api/stories/{seeded_story}/publish",
        headers=_auth(client._tok),
    )
    assert r.status == 200
    body = await r.json()
    assert body["published"] is False


async def test_get_publish_for_other_user_returns_404(client):
    """A story owned by a different user is not visible here."""
    other_id = "s-other"
    await client._db.enqueue(
        """
        INSERT INTO stories(
            id, author_user_id, story_date, audience_kind, audience_json,
            created_at, expires_at
        ) VALUES(?,?,?,?,?, datetime('now'), ?)
        """,
        (other_id, "u-bob", "2026-05-03", "all_paired", "[]", _expires()),
    )
    r = await client.get(
        f"/api/stories/{other_id}/publish",
        headers=_auth(client._tok),
    )
    assert r.status == 404


# ── DELETE publish + token revoke ───────────────────────────────────────


async def test_unpublish_returns_ok(client, seeded_story, stub_service):
    r = await client.delete(
        f"/api/stories/{seeded_story}/publish",
        headers=_auth(client._tok),
    )
    assert r.status == 200
    assert (await r.json())["unpublished"] is True
    assert stub_service.unpublish_calls == [(seeded_story, client._uid)]


async def test_revoke_token(client, seeded_story, stub_service):
    r = await client.delete(
        f"/api/stories/{seeded_story}/publish/tok-A",
        headers=_auth(client._tok),
    )
    assert r.status == 200
    assert (await r.json())["revoked"] is True
    assert stub_service.revoke_calls == [(seeded_story, client._uid, "tok-A")]


async def test_revoke_token_failure_maps_to_502(
    client,
    seeded_story,
    stub_service,
):
    stub_service.revoke_raises = StoryPublicationError("GFS down")
    r = await client.delete(
        f"/api/stories/{seeded_story}/publish/tok-A",
        headers=_auth(client._tok),
    )
    assert r.status == 502


# ── OG thumbnail upload route ────────────────────────────────────────────


import base64 as _b64


async def test_og_upload_calls_service_and_returns_url(
    client, seeded_story, stub_service,
):
    """Add the upload method on the stub at fixture time."""
    captured: list[tuple] = []

    async def upload_og_thumbnail(story_id, user_id, *, jpeg_bytes):
        captured.append((story_id, user_id, jpeg_bytes))
        return "https://gfs.example/og.jpg"

    stub_service.upload_og_thumbnail = upload_og_thumbnail  # type: ignore[attr-defined]
    r = await client.post(
        f"/api/stories/{seeded_story}/publish/og",
        json={"image_b64": _b64.b64encode(b"\xff\xd8\xff" + b"\x00" * 16).decode("ascii")},
        headers=_auth(client._tok),
    )
    assert r.status == 200
    body = await r.json()
    assert body["url"] == "https://gfs.example/og.jpg"
    assert captured[0][0] == seeded_story
    assert captured[0][1] == client._uid
    assert captured[0][2][:3] == b"\xff\xd8\xff"


async def test_og_upload_missing_image_returns_422(client, seeded_story):
    r = await client.post(
        f"/api/stories/{seeded_story}/publish/og",
        json={},
        headers=_auth(client._tok),
    )
    assert r.status == 422


async def test_og_upload_bad_b64_returns_422(client, seeded_story):
    r = await client.post(
        f"/api/stories/{seeded_story}/publish/og",
        json={"image_b64": "not!!!base64!!!"},
        headers=_auth(client._tok),
    )
    assert r.status == 422


async def test_og_upload_unauthenticated_returns_401(client, seeded_story):
    r = await client.post(
        f"/api/stories/{seeded_story}/publish/og",
        json={"image_b64": "Zm9v"},
    )
    assert r.status == 401
