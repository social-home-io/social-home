"""Route tests for §11 pairing rides /federation/inbox/{id}.

The legacy ``/api/pairing/peer-{accept,confirm}`` routes are gone —
peer-accept / peer-confirm are now :data:`PAIRING_PEER_ACCEPT` /
:data:`PAIRING_PEER_CONFIRM` federation events dispatched off the
federation inbox URL before the §24.11 pipeline.
"""

from __future__ import annotations

from urllib.parse import urlparse

import orjson

from socialhome.app_keys import federation_repo_key, federation_service_key
from socialhome.crypto import (
    derive_instance_id,
    generate_identity_keypair,
    generate_x25519_keypair,
)
from socialhome.domain.federation import FederationEventType
from socialhome.federation.peer_pairing_client import sign_peer_body

from .conftest import _auth


def _inbox_path_from_url(inbox_url: str) -> str:
    """Map a full inbox URL to the path-only form the test client expects."""
    parsed = urlparse(inbox_url)
    return parsed.path or "/"


async def _generate_qr(client) -> dict:
    r = await client.post(
        "/api/pairing/initiate",
        json={},
        headers=_auth(client._tok),
    )
    assert r.status in (200, 201)
    return await r.json()


def _peer_accept_body(*, token: str, b_id_pk: bytes, b_dh_pk: bytes) -> dict:
    return {
        "event_type": FederationEventType.PAIRING_PEER_ACCEPT.value,
        "token": token,
        "verification_code": "123456",
        "identity_pk": b_id_pk.hex(),
        "instance_id": derive_instance_id(b_id_pk),
        "dh_pk": b_dh_pk.hex(),
        "inbox_url": "https://peer.example/federation/inbox/wh-b",
        "display_name": "Peer B",
    }


async def test_peer_accept_via_inbox_happy_path(client):
    """Well-formed peer-accept materialises the RemoteInstance."""
    qr = await _generate_qr(client)
    path = _inbox_path_from_url(qr["inbox_url"])

    b_id_kp = generate_identity_keypair()
    b_dh_kp = generate_x25519_keypair()
    body = _peer_accept_body(
        token=qr["token"],
        b_id_pk=b_id_kp.public_key,
        b_dh_pk=b_dh_kp.public_key,
    )
    signed = sign_peer_body(body, own_identity_seed=b_id_kp.private_key)

    # No auth header — the federation inbox path is public.
    r = await client.post(
        path,
        data=orjson.dumps(signed),
        headers={"Content-Type": "application/json"},
    )
    assert r.status == 200
    data = await r.json()
    assert data["ok"] is True
    assert data["instance_id"] == derive_instance_id(b_id_kp.public_key)
    assert data["replay"] is False

    fed_repo = client.app[federation_repo_key]
    instances = await fed_repo.list_instances()
    assert len(instances) == 1
    assert instances[0].remote_inbox_url == body["inbox_url"]
    assert instances[0].status.value == "pending_received"


async def test_peer_accept_via_inbox_replay_is_idempotent(client):
    qr = await _generate_qr(client)
    path = _inbox_path_from_url(qr["inbox_url"])

    b_id_kp = generate_identity_keypair()
    b_dh_kp = generate_x25519_keypair()
    body = _peer_accept_body(
        token=qr["token"],
        b_id_pk=b_id_kp.public_key,
        b_dh_pk=b_dh_kp.public_key,
    )
    signed = sign_peer_body(body, own_identity_seed=b_id_kp.private_key)

    r1 = await client.post(
        path,
        data=orjson.dumps(signed),
        headers={"Content-Type": "application/json"},
    )
    assert r1.status == 200
    assert (await r1.json())["replay"] is False

    r2 = await client.post(
        path,
        data=orjson.dumps(signed),
        headers={"Content-Type": "application/json"},
    )
    assert r2.status == 200
    assert (await r2.json())["replay"] is True

    fed_repo = client.app[federation_repo_key]
    assert len(await fed_repo.list_instances()) == 1


async def test_peer_accept_via_inbox_rejects_missing_fields(client):
    """A ``PAIRING_PEER_ACCEPT`` body missing required fields → 400 from
    the pairing dispatch (never falls through to the §24.11 pipeline)."""
    r = await client.post(
        "/federation/inbox/anything",
        data=orjson.dumps(
            {
                "event_type": FederationEventType.PAIRING_PEER_ACCEPT.value,
                "token": "x",
            },
        ),
        headers={"Content-Type": "application/json"},
    )
    assert r.status == 400


async def test_peer_accept_via_inbox_rejects_unknown_token(client):
    b_id_kp = generate_identity_keypair()
    b_dh_kp = generate_x25519_keypair()
    body = {
        "event_type": FederationEventType.PAIRING_PEER_ACCEPT.value,
        "token": "not-a-real-token",
        "verification_code": "000000",
        "identity_pk": b_id_kp.public_key.hex(),
        "dh_pk": b_dh_kp.public_key.hex(),
        "inbox_url": "https://peer/wh",
    }
    signed = sign_peer_body(body, own_identity_seed=b_id_kp.private_key)
    r = await client.post(
        "/federation/inbox/anything",
        data=orjson.dumps(signed),
        headers={"Content-Type": "application/json"},
    )
    assert r.status == 404


async def test_peer_accept_via_inbox_rejects_bad_signature(client):
    qr = await _generate_qr(client)
    path = _inbox_path_from_url(qr["inbox_url"])

    b_id_kp = generate_identity_keypair()
    b_dh_kp = generate_x25519_keypair()
    body = {
        "event_type": FederationEventType.PAIRING_PEER_ACCEPT.value,
        "token": qr["token"],
        "verification_code": "123456",
        "identity_pk": b_id_kp.public_key.hex(),
        "dh_pk": b_dh_kp.public_key.hex(),
        "inbox_url": "https://peer/wh",
        "signature": "00" * 64,  # garbage
    }
    r = await client.post(
        path,
        data=orjson.dumps(body),
        headers={"Content-Type": "application/json"},
    )
    assert r.status == 403


async def test_peer_accept_via_inbox_rejects_instance_id_mismatch(client):
    qr = await _generate_qr(client)
    path = _inbox_path_from_url(qr["inbox_url"])

    b_id_kp = generate_identity_keypair()
    b_dh_kp = generate_x25519_keypair()
    body = {
        "event_type": FederationEventType.PAIRING_PEER_ACCEPT.value,
        "token": qr["token"],
        "verification_code": "123456",
        "identity_pk": b_id_kp.public_key.hex(),
        "instance_id": "not-derived-from-the-pk",
        "dh_pk": b_dh_kp.public_key.hex(),
        "inbox_url": "https://peer/wh",
    }
    signed = sign_peer_body(body, own_identity_seed=b_id_kp.private_key)
    r = await client.post(
        path,
        data=orjson.dumps(signed),
        headers={"Content-Type": "application/json"},
    )
    assert r.status == 403


async def test_peer_confirm_via_inbox_requires_fields(client):
    """``PAIRING_PEER_CONFIRM`` with no fields → 400."""
    r = await client.post(
        "/federation/inbox/anything",
        data=orjson.dumps(
            {"event_type": FederationEventType.PAIRING_PEER_CONFIRM.value},
        ),
        headers={"Content-Type": "application/json"},
    )
    assert r.status == 400


async def test_peer_confirm_via_inbox_unknown_token(client):
    a_id_kp = generate_identity_keypair()
    body = {
        "event_type": FederationEventType.PAIRING_PEER_CONFIRM.value,
        "token": "nope",
        "instance_id": "iid",
    }
    signed = sign_peer_body(body, own_identity_seed=a_id_kp.private_key)
    r = await client.post(
        "/federation/inbox/anything",
        data=orjson.dumps(signed),
        headers={"Content-Type": "application/json"},
    )
    assert r.status == 404


async def test_pairing_event_dispatch_skips_pipeline_for_unknown_inbox(client):
    """A ``PAIRING_PEER_ACCEPT`` body succeeds (or fails on its own
    grounds) regardless of whether ``{inbox_id}`` in the URL maps to
    a known peer — the §24.11 pipeline's instance lookup never runs."""
    qr = await _generate_qr(client)

    b_id_kp = generate_identity_keypair()
    b_dh_kp = generate_x25519_keypair()
    body = _peer_accept_body(
        token=qr["token"],
        b_id_pk=b_id_kp.public_key,
        b_dh_pk=b_dh_kp.public_key,
    )
    signed = sign_peer_body(body, own_identity_seed=b_id_kp.private_key)

    # POST to a deliberately-wrong inbox_id — pairing dispatch keys off
    # the body's ``token`` not the URL path, so this still succeeds.
    r = await client.post(
        "/federation/inbox/wrong-inbox-id",
        data=orjson.dumps(signed),
        headers={"Content-Type": "application/json"},
    )
    assert r.status == 200


async def test_non_pairing_event_falls_through_to_pipeline(client):
    """A body with a non-pairing ``event_type`` runs through the full
    §24.11 pipeline — proven by getting a normal 404/400 rejection
    instead of pairing's 200 happy path."""
    r = await client.post(
        "/federation/inbox/wh-nope",
        data=orjson.dumps(
            {
                "msg_id": "x",
                "event_type": "presence_updated",
                "from_instance": "a",
                "to_instance": "b",
                "timestamp": "2026-01-01T00:00:00+00:00",
                "encrypted_payload": "x:y",
                "sig_suite": "ed25519",
                "signatures": {"ed25519": "z"},
            },
        ),
        headers={"Content-Type": "application/json"},
    )
    assert r.status in (400, 404, 410)


async def test_pairing_dispatch_no_auth_header_required(client):
    """Sanity: the federation inbox path is public — a pairing event
    body with no ``Authorization`` header reaches the handler (which
    rejects on its own for missing fields).
    """
    r = await client.post(
        "/federation/inbox/anything",
        data=orjson.dumps(
            {"event_type": FederationEventType.PAIRING_PEER_ACCEPT.value},
        ),
        headers={"Content-Type": "application/json"},
        # explicitly no Authorization header
    )
    # 400 (missing fields), not 401 (would mean auth blocked us).
    assert r.status == 400


async def test_coordinator_exposes_peer_pairing_client(client):
    """The service has wired the outbound client so ``accept`` /
    ``confirm`` can deliver peer-accept / peer-confirm."""
    svc = client.app[federation_service_key]
    assert svc._pairing._peer_pairing_client is not None
