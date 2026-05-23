"""Route tests for the HA integration bridge — /api/ha/integration/* (§7, §11)."""

from __future__ import annotations

import json

from socialhome.app_keys import (
    db_key as _db_key,
    federation_repo_key,
    federation_service_key,
    url_update_outbound_key,
)
from socialhome.domain.federation import (
    InstanceSource,
    PairingStatus,
    RemoteInstance,
)

from .conftest import _auth


def _peer(iid: str, local_inbox_id: str) -> RemoteInstance:
    return RemoteInstance(
        id=iid,
        display_name=iid,
        remote_identity_pk="aa" * 32,
        key_self_to_remote="enc",
        key_remote_to_self="enc",
        remote_inbox_url=f"https://peer/{iid}",
        local_inbox_id=local_inbox_id,
        status=PairingStatus.CONFIRMED,
        source=InstanceSource.MANUAL,
    )


async def test_put_base_persists_and_reads_back(client):
    r = await client.put(
        "/api/ha/integration/federation-base",
        json={"base": "https://xx.ui.nabu.casa/api/social_home/inbox"},
        headers=_auth(client._tok),
    )
    assert r.status == 200
    body = await r.json()
    assert body["ok"] is True
    assert body["base"] == "https://xx.ui.nabu.casa/api/social_home/inbox"
    assert body["changed"] is True
    # First push has no existing peers → 0 notified
    assert body["peers_notified"] == 0

    # Round-trip GET returns the same value.
    r = await client.get(
        "/api/ha/integration/federation-base",
        headers=_auth(client._tok),
    )
    assert r.status == 200
    assert (await r.json())["base"] == "https://xx.ui.nabu.casa/api/social_home/inbox"


async def test_put_base_idempotent_when_unchanged(client):
    await client.put(
        "/api/ha/integration/federation-base",
        json={"base": "https://example/api/social_home/inbox"},
        headers=_auth(client._tok),
    )
    r = await client.put(
        "/api/ha/integration/federation-base",
        json={"base": "https://example/api/social_home/inbox"},
        headers=_auth(client._tok),
    )
    body = await r.json()
    assert body["changed"] is False
    assert body["peers_notified"] == 0


async def test_put_base_strips_trailing_slash(client):
    r = await client.put(
        "/api/ha/integration/federation-base",
        json={"base": "https://example/api/social_home/inbox/"},
        headers=_auth(client._tok),
    )
    body = await r.json()
    assert body["base"] == "https://example/api/social_home/inbox"


async def test_put_base_rejects_missing(client):
    r = await client.put(
        "/api/ha/integration/federation-base",
        json={},
        headers=_auth(client._tok),
    )
    assert r.status == 422


async def test_put_base_rejects_bad_scheme(client):
    r = await client.put(
        "/api/ha/integration/federation-base",
        json={"base": "ftp://nope.example/x"},
        headers=_auth(client._tok),
    )
    assert r.status == 422


async def test_put_base_rejects_empty_string(client):
    r = await client.put(
        "/api/ha/integration/federation-base",
        json={"base": "  "},
        headers=_auth(client._tok),
    )
    assert r.status == 422


async def test_put_base_fans_out_url_updated_to_confirmed_peers(client):
    """Seed two confirmed peers, push a new base, expect fan-out."""
    fed_repo = client.app[federation_repo_key]
    await fed_repo.save_instance(_peer("peer-a", "wh-a"))
    await fed_repo.save_instance(_peer("peer-b", "wh-b"))

    # Swap the outbound service with a recorder so we don't need a real
    # transport. The wiring is tested by
    # ``test_outbound_service_wired_on_app``; here we only care that the
    # route calls publish() with the right base.
    captured: list[str] = []

    class _RecordingOutbound:
        async def publish(self, *, new_inbox_base_url: str) -> int:
            captured.append(new_inbox_base_url)
            return 2

    client.app[url_update_outbound_key] = _RecordingOutbound()

    r = await client.put(
        "/api/ha/integration/federation-base",
        json={"base": "https://new.example/api/social_home/inbox"},
        headers=_auth(client._tok),
    )
    body = await r.json()
    assert body["peers_notified"] == 2
    assert captured == ["https://new.example/api/social_home/inbox"]


async def test_get_base_returns_null_when_unset(client):
    r = await client.get(
        "/api/ha/integration/federation-base",
        headers=_auth(client._tok),
    )
    assert r.status == 200
    assert (await r.json())["base"] is None


async def test_get_base_requires_admin(client):
    """Non-admin user cannot read the base."""
    # Demote the admin in the seeded test client.
    db = client.app[_db_key]
    await db.enqueue(
        "UPDATE users SET is_admin=0 WHERE user_id=?",
        (client._uid,),
    )
    r = await client.get(
        "/api/ha/integration/federation-base",
        headers=_auth(client._tok),
    )
    assert r.status == 403


async def test_outbound_service_wired_on_app(client):
    """The UrlUpdateOutbound service is registered under url_update_outbound_key."""
    assert client.app.get(url_update_outbound_key) is not None


# ── /api/ha/integration/ice-servers ─────────────────────────────────────


_VALID_ICE = [
    {"urls": ["stun:stun.example.org:3478"]},
    {
        "urls": [
            "turn:turn.example.org:3478",
            "turns:turn.example.org:5349",
        ],
        "username": "user",
        "credential": "secret",
    },
]


async def test_put_ice_servers_persists_and_pushes_to_federation(client):
    fed = client.app[federation_service_key]
    r = await client.put(
        "/api/ha/integration/ice-servers",
        json={"ice_servers": _VALID_ICE},
        headers=_auth(client._tok),
    )
    assert r.status == 200
    body = await r.json()
    assert body["ok"] is True
    assert body["changed"] is True
    assert body["ice_servers"] == _VALID_ICE

    # Live FederationService picked up the new list.
    assert fed._ice_servers == _VALID_ICE

    # Persisted to instance_config so a reboot replays.
    db = client.app[_db_key]
    row = await db.fetchone(
        "SELECT value FROM instance_config WHERE key=?",
        ("ha_ice_servers",),
    )
    assert row is not None
    assert json.loads(row["value"]) == _VALID_ICE

    # Round-trip GET returns the same list.
    r = await client.get(
        "/api/ha/integration/ice-servers",
        headers=_auth(client._tok),
    )
    assert r.status == 200
    assert (await r.json())["ice_servers"] == _VALID_ICE


async def test_put_ice_servers_idempotent_when_unchanged(client):
    await client.put(
        "/api/ha/integration/ice-servers",
        json={"ice_servers": _VALID_ICE},
        headers=_auth(client._tok),
    )
    r = await client.put(
        "/api/ha/integration/ice-servers",
        json={"ice_servers": _VALID_ICE},
        headers=_auth(client._tok),
    )
    body = await r.json()
    assert body["changed"] is False


async def test_put_ice_servers_accepts_string_urls_and_normalizes(client):
    """Chrome accepts ``urls`` as either a string or list — we normalize."""
    r = await client.put(
        "/api/ha/integration/ice-servers",
        json={
            "ice_servers": [{"urls": "stun:stun.example.org:3478"}],
        },
        headers=_auth(client._tok),
    )
    assert r.status == 200
    body = await r.json()
    assert body["ice_servers"] == [{"urls": ["stun:stun.example.org:3478"]}]


async def test_put_ice_servers_accepts_singular_url_field(client):
    """REGRESSION: HA core's ``webrtc_models`` library serialises the
    field name in the SINGULAR as ``url`` (pre-2017 Chrome dialect).
    Accepting both shapes prevents the integration's TURN push from
    failing 422 because of a field-name typo the operator has no
    way to fix from their side."""
    r = await client.put(
        "/api/ha/integration/ice-servers",
        json={
            "ice_servers": [
                {"url": "stun:stun.example.org:3478"},
                {
                    "url": "turn:turn.example.org:3478",
                    "username": "u",
                    "credential": "c",
                },
            ],
        },
        headers=_auth(client._tok),
    )
    assert r.status == 200
    body = await r.json()
    # Normalized back to the canonical ``urls`` list shape on storage.
    assert body["ice_servers"] == [
        {"urls": ["stun:stun.example.org:3478"]},
        {
            "urls": ["turn:turn.example.org:3478"],
            "username": "u",
            "credential": "c",
        },
    ]


async def test_put_ice_servers_422_echoes_specific_reason(client):
    """An operator pushing a malformed payload gets a 422 body
    explaining WHICH entry failed and HOW — not just a generic
    "ice_servers must be a list of …". Pre-fix the integration's UI
    couldn't surface the actual error so an operator just saw the
    push fail silently."""
    r = await client.put(
        "/api/ha/integration/ice-servers",
        json={
            "ice_servers": [
                {"urls": ["http://not-a-stun-or-turn.example/"]},
            ],
        },
        headers=_auth(client._tok),
    )
    assert r.status == 422
    body = await r.json()
    # New diagnostic message: names the entry index + the offending URL.
    assert "entry 0" in body["error"]["detail"]
    assert "http://not-a-stun-or-turn.example/" in body["error"]["detail"]


async def test_put_ice_servers_422_for_too_many_servers(client):
    """Bound-check error message should report the cap so an
    operator can see they're hitting the per-PUT limit."""
    r = await client.put(
        "/api/ha/integration/ice-servers",
        json={
            "ice_servers": [
                {"urls": [f"stun:stun.example.org:{3000 + i}"]}
                for i in range(20)  # well over the 8-server cap
            ],
        },
        headers=_auth(client._tok),
    )
    assert r.status == 422
    body = await r.json()
    assert "too many servers" in body["error"]["detail"]


async def test_put_ice_servers_clears_to_empty_list(client):
    """Operator can clear the override by pushing ``[]``."""
    await client.put(
        "/api/ha/integration/ice-servers",
        json={"ice_servers": _VALID_ICE},
        headers=_auth(client._tok),
    )
    r = await client.put(
        "/api/ha/integration/ice-servers",
        json={"ice_servers": []},
        headers=_auth(client._tok),
    )
    assert r.status == 200
    body = await r.json()
    assert body["ice_servers"] == []
    assert client.app[federation_service_key]._ice_servers == []


async def test_put_ice_servers_rejects_non_list(client):
    r = await client.put(
        "/api/ha/integration/ice-servers",
        json={"ice_servers": {"urls": ["stun:x"]}},
        headers=_auth(client._tok),
    )
    assert r.status == 422


async def test_put_ice_servers_rejects_bad_scheme(client):
    r = await client.put(
        "/api/ha/integration/ice-servers",
        json={"ice_servers": [{"urls": ["http://relay.example/turn"]}]},
        headers=_auth(client._tok),
    )
    assert r.status == 422


async def test_put_ice_servers_rejects_missing_urls(client):
    r = await client.put(
        "/api/ha/integration/ice-servers",
        json={"ice_servers": [{"username": "u", "credential": "p"}]},
        headers=_auth(client._tok),
    )
    assert r.status == 422


async def test_put_ice_servers_rejects_empty_urls_list(client):
    r = await client.put(
        "/api/ha/integration/ice-servers",
        json={"ice_servers": [{"urls": []}]},
        headers=_auth(client._tok),
    )
    assert r.status == 422


async def test_put_ice_servers_rejects_non_string_credential(client):
    r = await client.put(
        "/api/ha/integration/ice-servers",
        json={
            "ice_servers": [
                {"urls": ["turn:x"], "username": "u", "credential": 12345},
            ],
        },
        headers=_auth(client._tok),
    )
    assert r.status == 422


async def test_put_ice_servers_rejects_too_many_servers(client):
    too_many = [{"urls": [f"stun:s{i}.example:3478"]} for i in range(9)]
    r = await client.put(
        "/api/ha/integration/ice-servers",
        json={"ice_servers": too_many},
        headers=_auth(client._tok),
    )
    assert r.status == 422


async def test_get_ice_servers_returns_empty_when_unset(client):
    r = await client.get(
        "/api/ha/integration/ice-servers",
        headers=_auth(client._tok),
    )
    assert r.status == 200
    assert (await r.json())["ice_servers"] == []


async def test_get_ice_servers_requires_admin(client):
    db = client.app[_db_key]
    await db.enqueue(
        "UPDATE users SET is_admin=0 WHERE user_id=?",
        (client._uid,),
    )
    r = await client.get(
        "/api/ha/integration/ice-servers",
        headers=_auth(client._tok),
    )
    assert r.status == 403


async def test_put_ice_servers_requires_admin(client):
    db = client.app[_db_key]
    await db.enqueue(
        "UPDATE users SET is_admin=0 WHERE user_id=?",
        (client._uid,),
    )
    r = await client.put(
        "/api/ha/integration/ice-servers",
        json={"ice_servers": _VALID_ICE},
        headers=_auth(client._tok),
    )
    assert r.status == 403


async def test_boot_replays_persisted_ice_servers(tmp_dir, aiohttp_client):
    """A second app boot on the same data_dir replays the operator-pushed
    ICE list into the live FederationService before the first peer
    handshake fires.
    """
    from types import MappingProxyType

    from socialhome.app import create_app
    from socialhome.config import Config

    cfg = Config(
        data_dir=str(tmp_dir),
        db_path=str(tmp_dir / "test.db"),
        media_path=str(tmp_dir / "media"),
        mode="standalone",
        log_level="WARNING",
        db_write_batch_timeout_ms=10,
        platform_options=MappingProxyType(
            {
                "standalone": MappingProxyType(
                    {"external_url": "https://test.example"},
                ),
            },
        ),
    )

    # First boot: seed the persisted row via the public API surface
    # (instance_config), then tear down.
    app1 = create_app(cfg)
    tc1 = await aiohttp_client(app1)
    await tc1.app[_db_key].enqueue(
        "INSERT INTO instance_config(key, value) VALUES(?,?)"
        " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        ("ha_ice_servers", json.dumps(_VALID_ICE)),
    )
    await tc1.close()

    # Second boot on the same data_dir: federation_service must already
    # see the persisted list (no PUT in this run).
    app2 = create_app(cfg)
    tc2 = await aiohttp_client(app2)
    try:
        assert tc2.app[federation_service_key]._ice_servers == _VALID_ICE
    finally:
        await tc2.close()


async def test_get_ice_servers_recovers_from_corrupt_persisted_value(client):
    """A hand-edited / migrated bad row must not 500 the GET — just reset."""
    db = client.app[_db_key]
    await db.enqueue(
        "INSERT INTO instance_config(key, value) VALUES(?,?)"
        " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        ("ha_ice_servers", "this is not json"),
    )
    r = await client.get(
        "/api/ha/integration/ice-servers",
        headers=_auth(client._tok),
    )
    assert r.status == 200
    assert (await r.json())["ice_servers"] == []
