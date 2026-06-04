"""Tests for ``PATCH /api/admin/instance`` (rename the household).

Admin-only endpoint that persists ``instance_identity.display_name`` and
re-broadcasts it to confirmed peers via INSTANCE_CAPABILITIES_UPDATED.
"""

from __future__ import annotations

from socialhome.app_keys import capabilities_outbound_key, federation_repo_key

from .conftest import _auth


class _RecordingOutbound:
    """Stub ``capabilities_outbound`` recording ``publish()`` calls."""

    def __init__(self) -> None:
        self.calls = 0

    async def publish(self) -> int:
        self.calls += 1
        return 0


async def test_requires_admin(client):
    """A non-admin token gets 403."""
    db = client._db
    await db.enqueue(
        "UPDATE users SET is_admin=0 WHERE user_id=?",
        (client._uid,),
    )
    resp = await client.patch(
        "/api/admin/instance",
        headers=_auth(client._tok),
        json={"display_name": "Casa Vizeli"},
    )
    assert resp.status == 403


async def test_renames_and_persists(client):
    """Admin PATCH returns the new name and persists it to the DB."""
    resp = await client.patch(
        "/api/admin/instance",
        headers=_auth(client._tok),
        json={"display_name": "Casa Vizeli"},
    )
    assert resp.status == 200
    body = await resp.json()
    assert body == {"display_name": "Casa Vizeli"}
    identity = await client.app[federation_repo_key].get_local_identity()
    assert identity["display_name"] == "Casa Vizeli"


async def test_triggers_broadcast(client):
    """A successful rename re-broadcasts capabilities exactly once."""
    stub = _RecordingOutbound()
    client.app[capabilities_outbound_key] = stub
    resp = await client.patch(
        "/api/admin/instance",
        headers=_auth(client._tok),
        json={"display_name": "Casa Vizeli"},
    )
    assert resp.status == 200
    assert stub.calls == 1


async def test_rejects_blank(client):
    """A blank/whitespace name is rejected with 422."""
    resp = await client.patch(
        "/api/admin/instance",
        headers=_auth(client._tok),
        json={"display_name": "   "},
    )
    assert resp.status == 422


async def test_rejects_too_long(client):
    """A name longer than 80 chars is rejected with 422."""
    resp = await client.patch(
        "/api/admin/instance",
        headers=_auth(client._tok),
        json={"display_name": "x" * 81},
    )
    assert resp.status == 422
