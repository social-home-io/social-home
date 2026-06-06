"""Tests for the global-space publish surface (``GET /gfs/spaces/{id}``,
``POST /gfs/spaces/{id}/publish``, ``DELETE /gfs/spaces/{id}/unpublish``)
plus the matching :class:`GfsFederationService` methods.

The publish wire flow is what an SH-side ``GfsConnectionService.publish_space``
hits when a household flips a local space to ``space_type=global``.
The GFS verifies the Ed25519 signature against the registered
``ClientInstance.public_key`` and upserts a ``GlobalSpace`` row at
``status='active'`` (or ``pending`` if the GFS admin has
``auto_accept_clients=0``).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from aiohttp.test_utils import TestClient, TestServer

from socialhome.crypto import (
    b64url_encode,
    generate_identity_keypair,
    sign_ed25519,
)
from socialhome.global_server import create_gfs_app
from socialhome.global_server.app_keys import (
    gfs_fed_repo_key,
)
from socialhome.global_server.domain import ClientInstance, GlobalSpace


@pytest.fixture
async def gfs_client(tmp_path):
    app = create_gfs_app(db_path=tmp_path / "gfs.db")
    async with TestClient(TestServer(app)) as tc:
        yield tc


async def _register_owner(
    app,
    *,
    instance_id: str = "owner.home",
    auto_accept: bool = True,
) -> tuple[bytes, bytes]:
    """Insert a ClientInstance row and return its (seed, public-key-bytes).

    Tests sign their own publish bodies with ``seed`` so the signature
    verifies against the stored ``ClientInstance.public_key``.
    """
    kp = generate_identity_keypair()
    fed_repo = app[gfs_fed_repo_key]
    await fed_repo.upsert_instance(
        ClientInstance(
            instance_id=instance_id,
            display_name=instance_id,
            public_key=kp.public_key.hex(),
            inbox_url="https://owner.example/federation/inbox/x",
            status="active",
            auto_accept=auto_accept,
        )
    )
    return kp.private_key, kp.public_key


def _sign_publish_body(body: dict, *, seed: bytes) -> dict:
    """Compute the canonical signature the route verifier expects.

    Mirrors the body-shape ``GfsFederationService.publish_space``
    canonicalises before verifying — sorted keys, no whitespace,
    no ``signature`` field included in the signed bytes.
    """
    canonical = json.dumps(body, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return {**body, "signature": b64url_encode(sign_ed25519(seed, canonical))}


# ── GET /gfs/spaces/{id} ────────────────────────────────────────────────


async def test_get_space_detail_returns_active_row(gfs_client):
    """A published, active space surfaces under ``GET /gfs/spaces/{id}``."""
    app = gfs_client.server.app
    await _register_owner(app)
    fed_repo = app[gfs_fed_repo_key]
    await fed_repo.upsert_space(
        GlobalSpace(
            space_id="sp-detail",
            owning_instance="owner.home",
            name="Detail Space",
            description="hello",
            cover_url="https://cdn.example/cover.jpg",
            status="active",
            published_at="2026-05-10T00:00:00Z",
        )
    )
    resp = await gfs_client.get("/gfs/spaces/sp-detail")
    assert resp.status == 200
    body = await resp.json()
    assert body["space_id"] == "sp-detail"
    assert body["name"] == "Detail Space"
    assert body["cover_url"] == "https://cdn.example/cover.jpg"
    assert body["status"] == "active"


async def test_get_space_detail_404_when_missing(gfs_client):
    resp = await gfs_client.get("/gfs/spaces/nope")
    assert resp.status == 404


async def test_get_space_detail_404_when_banned(gfs_client):
    """Banned rows stay in the DB (audit trail) but disappear from the
    public surface — the same rule the listing endpoint follows."""
    app = gfs_client.server.app
    await _register_owner(app)
    fed_repo = app[gfs_fed_repo_key]
    await fed_repo.upsert_space(
        GlobalSpace(
            space_id="sp-banned",
            owning_instance="owner.home",
            name="Banned",
            status="banned",
        )
    )
    resp = await gfs_client.get("/gfs/spaces/sp-banned")
    assert resp.status == 404


# ── POST /gfs/spaces/{id}/publish ───────────────────────────────────────


async def test_publish_space_happy_path_active(gfs_client):
    """Auto-accepted owner with a valid signature lands as ``active``
    and shows up on ``GET /gfs/spaces``."""
    seed, _pk = await _register_owner(gfs_client.server.app, auto_accept=True)
    body = _sign_publish_body(
        {
            "space_id": "sp-1",
            "owning_instance": "owner.home",
            "name": "Local Birds",
            "description": "everyday birds in the neighbourhood",
            "about_markdown": "",
            "cover_url": "",
            "min_age": 0,
            "target_audience": "all",
            "accent_color": "#D2542A",
            "icon_url": "",
            "primary_color": "#D2542A",
        },
        seed=seed,
    )
    resp = await gfs_client.post("/gfs/spaces/sp-1/publish", json=body)
    assert resp.status == 200
    payload = await resp.json()
    assert payload == {"status": "active", "space_id": "sp-1"}

    listing = await gfs_client.get("/gfs/spaces")
    items = (await listing.json())["spaces"]
    assert any(sp["space_id"] == "sp-1" and sp["name"] == "Local Birds" for sp in items)


async def test_publish_space_pending_when_auto_accept_off(gfs_client):
    """A registered-but-not-auto-accepted owner lands as ``pending``;
    the row exists in the DB but the public listing hides it."""
    app = gfs_client.server.app
    seed, _pk = await _register_owner(app, auto_accept=False)
    body = _sign_publish_body(
        {
            "space_id": "sp-pending",
            "owning_instance": "owner.home",
            "name": "Pending",
            "description": "",
            "about_markdown": "",
            "cover_url": "",
            "min_age": 0,
            "target_audience": "all",
            "accent_color": "#D2542A",
            "icon_url": "",
            "primary_color": "#D2542A",
        },
        seed=seed,
    )
    resp = await gfs_client.post("/gfs/spaces/sp-pending/publish", json=body)
    assert resp.status == 200
    payload = await resp.json()
    assert payload["status"] == "pending"

    listing = await gfs_client.get("/gfs/spaces")
    items = (await listing.json())["spaces"]
    assert all(sp["space_id"] != "sp-pending" for sp in items)


async def test_publish_space_missing_field_400(gfs_client):
    """``name`` is required by the route — missing it returns 400 even
    before signature verification kicks in."""
    seed, _pk = await _register_owner(gfs_client.server.app)
    body = _sign_publish_body(
        {
            "space_id": "sp-bad",
            "owning_instance": "owner.home",
            # ``name`` deliberately missing
        },
        seed=seed,
    )
    resp = await gfs_client.post("/gfs/spaces/sp-bad/publish", json=body)
    assert resp.status == 400


async def test_publish_space_unknown_instance_403(gfs_client):
    """An owner the GFS hasn't registered cannot publish — 403."""
    body = {
        "space_id": "sp-unknown",
        "owning_instance": "ghost.home",
        "name": "Ghost",
        "signature": "AAAA",
    }
    resp = await gfs_client.post("/gfs/spaces/sp-unknown/publish", json=body)
    assert resp.status == 403
    err = await resp.json()
    assert "ghost.home" in err["error"]


async def test_publish_space_invalid_signature_403(gfs_client):
    """The owner is known but the body signature fails verification —
    403 with ``Invalid Ed25519 signature``."""
    await _register_owner(gfs_client.server.app)
    # Sign with a *different* keypair than the one we registered on
    # the server. The body shape is otherwise valid; the verify call
    # is what trips.
    other_seed = generate_identity_keypair().private_key
    body = _sign_publish_body(
        {
            "space_id": "sp-badsig",
            "owning_instance": "owner.home",
            "name": "Wrong Key",
            "description": "",
            "about_markdown": "",
            "cover_url": "",
            "min_age": 0,
            "target_audience": "all",
            "accent_color": "#D2542A",
            "icon_url": "",
            "primary_color": "#D2542A",
        },
        seed=other_seed,
    )
    resp = await gfs_client.post("/gfs/spaces/sp-badsig/publish", json=body)
    assert resp.status == 403
    err = await resp.json()
    assert "signature" in err["error"].lower()


async def test_publish_space_preserves_subscriber_count_across_publishes(gfs_client):
    """Subscriber count + posts_per_week + published_at belong to the
    GFS — a re-publish with new metadata must not zero them out."""
    app = gfs_client.server.app
    seed, _pk = await _register_owner(app)
    fed_repo = app[gfs_fed_repo_key]
    # Seed a row with non-zero counts.
    await fed_repo.upsert_space(
        GlobalSpace(
            space_id="sp-keep",
            owning_instance="owner.home",
            name="Old Name",
            status="active",
            subscriber_count=42,
            posts_per_week=3.5,
            published_at="2026-05-01T00:00:00Z",
        )
    )
    body = _sign_publish_body(
        {
            "space_id": "sp-keep",
            "owning_instance": "owner.home",
            "name": "New Name",
            "description": "freshly renamed",
            "about_markdown": "",
            "cover_url": "",
            "min_age": 0,
            "target_audience": "all",
            "accent_color": "#D2542A",
            "icon_url": "",
            "primary_color": "#D2542A",
        },
        seed=seed,
    )
    resp = await gfs_client.post("/gfs/spaces/sp-keep/publish", json=body)
    assert resp.status == 200
    detail = await (await gfs_client.get("/gfs/spaces/sp-keep")).json()
    assert detail["name"] == "New Name"
    assert detail["subscriber_count"] == 42
    assert detail["posts_per_week"] == 3.5
    assert detail["published_at"] == "2026-05-01T00:00:00Z"


async def test_publish_space_cannot_unban_a_banned_row(gfs_client):
    """A banned space stays banned even if the owner re-publishes —
    only the GFS admin can lift the ban (see admin portal)."""
    app = gfs_client.server.app
    seed, _pk = await _register_owner(app, auto_accept=True)
    fed_repo = app[gfs_fed_repo_key]
    await fed_repo.upsert_space(
        GlobalSpace(
            space_id="sp-banned-2",
            owning_instance="owner.home",
            name="Banned",
            status="banned",
        )
    )
    body = _sign_publish_body(
        {
            "space_id": "sp-banned-2",
            "owning_instance": "owner.home",
            "name": "Re-Published",
            "description": "",
            "about_markdown": "",
            "cover_url": "",
            "min_age": 0,
            "target_audience": "all",
            "accent_color": "#D2542A",
            "icon_url": "",
            "primary_color": "#D2542A",
        },
        seed=seed,
    )
    resp = await gfs_client.post("/gfs/spaces/sp-banned-2/publish", json=body)
    assert resp.status == 200
    payload = await resp.json()
    assert payload["status"] == "banned"


# ── DELETE /gfs/spaces/{id}/unpublish ───────────────────────────────────


async def test_unpublish_space_flips_to_banned(gfs_client):
    """Unpublish flips the row to ``banned`` (so it disappears from
    ``GET /gfs/spaces`` + ``GET /gfs/spaces/{id}``) but keeps the row
    so the GFS admin's audit trail survives."""
    app = gfs_client.server.app
    await _register_owner(app)
    fed_repo = app[gfs_fed_repo_key]
    await fed_repo.upsert_space(
        GlobalSpace(
            space_id="sp-bye",
            owning_instance="owner.home",
            name="Bye",
            status="active",
            subscriber_count=10,
        )
    )
    resp = await gfs_client.delete("/gfs/spaces/sp-bye/unpublish")
    assert resp.status == 200
    payload = await resp.json()
    assert payload == {"status": "unpublished"}
    # Public surface no longer sees it.
    assert (await gfs_client.get("/gfs/spaces/sp-bye")).status == 404
    # But the row is still there (status=banned, count preserved).
    sp = await fed_repo.get_space("sp-bye")
    assert sp is not None
    assert sp.status == "banned"
    assert sp.subscriber_count == 10


async def test_unpublish_space_idempotent_on_missing(gfs_client):
    """Unpublishing a space that's not registered is a no-op — same
    shape as the regular ``DELETE`` for absent resources elsewhere
    in the GFS API. Idempotent semantics let the SH-side
    ``unpublish_space_from_all`` retry safely after a partial fan-out
    without surfacing a transient 404 to the operator."""
    resp = await gfs_client.delete("/gfs/spaces/nope/unpublish")
    assert resp.status == 200
    payload = await resp.json()
    assert payload == {"status": "unpublished"}


async def test_unpublish_space_post_method_also_works(gfs_client):
    """The aiohttp router accepts both POST and DELETE on the unpublish
    endpoint (see ``SpaceUnpublishView`` — we accept either since some
    HTTP clients struggle with DELETE bodies)."""
    app = gfs_client.server.app
    await _register_owner(app)
    fed_repo = app[gfs_fed_repo_key]
    await fed_repo.upsert_space(
        GlobalSpace(
            space_id="sp-bye-2",
            owning_instance="owner.home",
            name="Bye2",
            status="active",
        )
    )
    resp = await gfs_client.post("/gfs/spaces/sp-bye-2/unpublish")
    assert resp.status == 200


# ── POST /gfs/instance (signed display-name update) ─────────────────────


def _sign_body(body: dict, *, seed: bytes) -> dict:
    canonical = json.dumps(body, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return {**body, "signature": b64url_encode(sign_ed25519(seed, canonical))}


async def test_instance_update_happy_path_200(gfs_client):
    """A registered instance renames itself with a valid signature + fresh ts."""
    app = gfs_client.server.app
    seed, _pk = await _register_owner(app, instance_id="rename.home")
    ts = datetime.now(timezone.utc).isoformat()
    body = _sign_body(
        {"instance_id": "rename.home", "display_name": "Fresh Name", "ts": ts},
        seed=seed,
    )
    resp = await gfs_client.post("/gfs/instance", json=body)
    assert resp.status == 200
    payload = await resp.json()
    assert payload == {"status": "ok", "instance_id": "rename.home"}

    stored = await app[gfs_fed_repo_key].get_instance("rename.home")
    assert stored is not None
    assert stored.display_name == "Fresh Name"


async def test_instance_update_unknown_instance_403(gfs_client):
    """An instance the GFS hasn't registered can't rename — 403."""
    ts = datetime.now(timezone.utc).isoformat()
    body = {
        "instance_id": "ghost.home",
        "display_name": "Ghost",
        "ts": ts,
        "signature": "AAAA",
    }
    resp = await gfs_client.post("/gfs/instance", json=body)
    assert resp.status == 403


async def test_instance_update_bad_signature_403(gfs_client):
    """A signature from the wrong key fails verification — 403."""
    app = gfs_client.server.app
    await _register_owner(app, instance_id="known.home")
    other_seed = generate_identity_keypair().private_key
    ts = datetime.now(timezone.utc).isoformat()
    body = _sign_body(
        {"instance_id": "known.home", "display_name": "Hijack", "ts": ts},
        seed=other_seed,
    )
    resp = await gfs_client.post("/gfs/instance", json=body)
    assert resp.status == 403
    err = await resp.json()
    assert "signature" in err["error"].lower()


async def test_instance_update_stale_timestamp_403(gfs_client):
    """A stale ts is rejected with 403 (replay guard)."""
    app = gfs_client.server.app
    seed, _pk = await _register_owner(app, instance_id="stale.home")
    ts = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    body = _sign_body(
        {"instance_id": "stale.home", "display_name": "Late", "ts": ts},
        seed=seed,
    )
    resp = await gfs_client.post("/gfs/instance", json=body)
    assert resp.status == 403


async def test_instance_update_overlong_name_422(gfs_client):
    """A >80-char display_name maps to 422."""
    app = gfs_client.server.app
    seed, _pk = await _register_owner(app, instance_id="long.home")
    ts = datetime.now(timezone.utc).isoformat()
    name = "z" * 81
    body = _sign_body(
        {"instance_id": "long.home", "display_name": name, "ts": ts},
        seed=seed,
    )
    resp = await gfs_client.post("/gfs/instance", json=body)
    assert resp.status == 422


async def test_instance_update_missing_field_400(gfs_client):
    """A missing required field is a 400 before any verification."""
    app = gfs_client.server.app
    await _register_owner(app, instance_id="incomplete.home")
    resp = await gfs_client.post(
        "/gfs/instance",
        json={"instance_id": "incomplete.home"},
    )
    assert resp.status == 400


async def test_publish_space_caps_about_markdown(gfs_client):
    """An oversized about_markdown is truncated at storage (DB-bloat /
    render-cost guard) — the signature still validates the full value."""
    from socialhome.global_server.federation import MAX_ABOUT_MARKDOWN_CHARS

    app = gfs_client.server.app
    seed, _pk = await _register_owner(app, auto_accept=True)
    big = "x" * (MAX_ABOUT_MARKDOWN_CHARS + 5000)
    body = _sign_publish_body(
        {
            "space_id": "sp-big",
            "owning_instance": "owner.home",
            "name": "Big",
            "description": "",
            "about_markdown": big,
            "cover_url": "",
            "min_age": 0,
            "target_audience": "all",
            "accent_color": "#D2542A",
            "icon_url": "",
            "primary_color": "#D2542A",
        },
        seed=seed,
    )
    resp = await gfs_client.post("/gfs/spaces/sp-big/publish", json=body)
    assert resp.status == 200
    stored = await app[gfs_fed_repo_key].get_space("sp-big")
    assert stored is not None
    assert len(stored.about_markdown) == MAX_ABOUT_MARKDOWN_CHARS
