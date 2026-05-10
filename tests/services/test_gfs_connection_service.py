"""Tests for GfsConnectionService + SqliteGfsConnectionRepo."""

from __future__ import annotations

import json

import pytest

from socialhome.crypto import derive_instance_id, generate_identity_keypair
from socialhome.db.database import AsyncDatabase
from socialhome.domain.federation import GfsConnection
from socialhome.repositories.gfs_connection_repo import SqliteGfsConnectionRepo
from socialhome.services.gfs_connection_service import (
    GfsConnectionError,
    GfsConnectionService,
)


# ─── Helpers ────────────────────────────────────────────────────────────


class _StubResp:
    __slots__ = ("status", "_body", "_text")

    def __init__(self, status: int, body: dict | None = None, text: str = ""):
        self.status = status
        self._body = body or {}
        self._text = text

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def json(self):
        return self._body

    async def text(self):
        return self._text


class _StubSession:
    """Stub aiohttp session.

    Per-method overrides land in ``method_responses`` (keyed by ``"GET"``
    / ``"POST"`` / etc.); fall back to the default ``status`` / ``body``
    when the method has no override.
    """

    __slots__ = ("_status", "_body", "method_responses", "calls", "_last_body")

    def __init__(
        self,
        *,
        status: int = 200,
        body: dict | None = None,
        method_responses: dict[str, tuple[int, dict]] | None = None,
    ):
        self._status = status
        self._body = body or {}
        self.method_responses = method_responses or {}
        self.calls: list[tuple[str, str]] = []
        # Last JSON body the caller passed via ``json=`` — exposed for
        # tests that need to assert what got serialized on the wire
        # (e.g. publish-body signature verification).
        self._last_body: dict | None = None

    def _resp(self, method: str) -> _StubResp:
        override = self.method_responses.get(method)
        if override is not None:
            status, body = override
            return _StubResp(status, body)
        return _StubResp(self._status, self._body)

    def get(self, url, **kw):
        self.calls.append(("GET", url))
        return self._resp("GET")

    def post(self, url, **kw):
        self.calls.append(("POST", url))
        if "json" in kw:
            self._last_body = kw["json"]
        return self._resp("POST")

    def delete(self, url, **kw):
        self.calls.append(("DELETE", url))
        return self._resp("DELETE")


# ─── Fixtures ───────────────────────────────────────────────────────────


@pytest.fixture
async def env(tmp_dir):
    kp = generate_identity_keypair()
    iid = derive_instance_id(kp.public_key)
    db = AsyncDatabase(tmp_dir / "t.db", batch_timeout_ms=10)
    await db.startup()
    await db.enqueue(
        "INSERT INTO instance_identity(instance_id, identity_private_key,"
        " identity_public_key, routing_secret) VALUES(?,?,?,?)",
        (iid, kp.private_key.hex(), kp.public_key.hex(), "aa" * 32),
    )
    repo = SqliteGfsConnectionRepo(db)
    yield db, repo
    await db.shutdown()


def _make_conn(
    gfs_id: str = "gfs-1",
    *,
    status: str = "active",
    inbox_url: str = "https://gfs.example.com",
) -> GfsConnection:
    return GfsConnection(
        id=gfs_id,
        gfs_instance_id=f"inst-{gfs_id}",
        display_name=f"GFS {gfs_id}",
        public_key="pubkey-hex",
        inbox_url=inbox_url,
        status=status,
        paired_at="2025-01-01T00:00:00+00:00",
    )


# ─── Repo tests ─────────────────────────────────────────────────────────


async def test_save_and_get(env):
    _, repo = env
    conn = _make_conn("gfs-1")
    await repo.save(conn)
    got = await repo.get("gfs-1")
    assert got is not None
    assert got.id == "gfs-1"
    assert got.gfs_instance_id == "inst-gfs-1"


async def test_get_nonexistent_returns_none(env):
    _, repo = env
    assert await repo.get("nope") is None


async def test_list_active_filters_status(env):
    _, repo = env
    await repo.save(_make_conn("a1", status="active"))
    await repo.save(_make_conn("a2", status="suspended"))
    await repo.save(_make_conn("a3", status="pending"))
    active = await repo.list_active()
    assert len(active) == 1
    assert active[0].id == "a1"


# ── Service: report_fraud ──────────────────────────────────────────────


async def test_report_fraud_signs_and_posts(env):
    _, repo = env
    await repo.save(_make_conn("gfs-1", status="active", inbox_url="https://gfs.test"))
    session = _StubSession(status=200, body={"status": "recorded"})
    svc = GfsConnectionService(repo, http_client=session)  # type: ignore[arg-type]
    ok = await svc.report_fraud(
        "gfs-1",
        target_type="space",
        target_id="s-1",
        category="spam",
        notes="bad",
        reporter_instance_id="me.home",
        reporter_user_id="u-1",
        signing_key=b"\x01" * 32,
    )
    assert ok is True
    assert session.calls and session.calls[0][0] == "POST"
    assert session.calls[0][1].endswith("/gfs/report")


async def test_report_fraud_returns_false_on_http_error(env):
    _, repo = env
    await repo.save(_make_conn("gfs-2", status="active", inbox_url="https://gfs.test"))
    session = _StubSession(
        status=500,
        body={},
    )
    svc = GfsConnectionService(repo, http_client=session)  # type: ignore[arg-type]
    ok = await svc.report_fraud(
        "gfs-2",
        target_type="instance",
        target_id="peer.home",
        category="spam",
        notes=None,
        reporter_instance_id="me.home",
        reporter_user_id=None,
        signing_key=b"\x02" * 32,
    )
    assert ok is False


async def test_report_fraud_returns_false_for_unknown_gfs(env):
    _, repo = env
    session = _StubSession(status=200)
    svc = GfsConnectionService(repo, http_client=session)  # type: ignore[arg-type]
    ok = await svc.report_fraud(
        "nope",
        target_type="space",
        target_id="s-1",
        category="spam",
        notes=None,
        reporter_instance_id="me.home",
        reporter_user_id=None,
        signing_key=b"\x03" * 32,
    )
    assert ok is False
    assert session.calls == []


async def test_disconnect_deletes_connection(env):
    _, repo = env
    await repo.save(_make_conn("rm-1"))
    svc = GfsConnectionService(repo, http_client=_StubSession())  # type: ignore[arg-type]
    await svc.disconnect("rm-1")
    assert await repo.get("rm-1") is None


async def test_disconnect_unknown_raises(env):
    _, repo = env
    svc = GfsConnectionService(repo, http_client=_StubSession())  # type: ignore[arg-type]
    with pytest.raises(GfsConnectionError):
        await svc.disconnect("nope")


async def test_publish_space_records_local(env):
    _, repo = env
    await repo.save(_make_conn("pub-1"))
    session = _StubSession(status=200)
    svc = GfsConnectionService(repo, http_client=session)  # type: ignore[arg-type]
    await svc.publish_space("space-x", "pub-1")
    # Post to publish endpoint happened.
    assert session.calls
    assert session.calls[0][0] == "POST"
    assert "/gfs/spaces/space-x/publish" in session.calls[0][1]


async def test_unpublish_space_records_local(env):
    _, repo = env
    await repo.save(_make_conn("up-1"))
    session = _StubSession(status=200)
    svc = GfsConnectionService(repo, http_client=session)  # type: ignore[arg-type]
    await svc.unpublish_space("space-y", "up-1")
    assert session.calls and session.calls[0][0] == "DELETE"


async def test_update_status(env):
    _, repo = env
    await repo.save(_make_conn("gfs-1"))
    await repo.update_status("gfs-1", "suspended")
    got = await repo.get("gfs-1")
    assert got is not None
    assert got.status == "suspended"


async def test_delete_removes_connection_and_publications(env):
    _, repo = env
    await repo.save(_make_conn("gfs-1"))
    await repo.publish_space("sp-1", "gfs-1")
    await repo.delete("gfs-1")
    assert await repo.get("gfs-1") is None
    pubs = await repo.list_publications("gfs-1")
    assert pubs == []


async def test_publish_and_unpublish_space(env):
    _, repo = env
    await repo.save(_make_conn("gfs-1"))
    await repo.publish_space("sp-1", "gfs-1")
    pubs = await repo.list_publications("gfs-1")
    assert len(pubs) == 1
    assert pubs[0].space_id == "sp-1"

    await repo.unpublish_space("sp-1", "gfs-1")
    pubs = await repo.list_publications("gfs-1")
    assert pubs == []


async def test_publish_space_idempotent(env):
    _, repo = env
    await repo.save(_make_conn("gfs-1"))
    await repo.publish_space("sp-1", "gfs-1")
    await repo.publish_space("sp-1", "gfs-1")
    pubs = await repo.list_publications("gfs-1")
    assert len(pubs) == 1


async def test_list_gfs_for_space(env):
    _, repo = env
    await repo.save(_make_conn("gfs-1"))
    await repo.save(_make_conn("gfs-2"))
    await repo.publish_space("sp-1", "gfs-1")
    await repo.publish_space("sp-1", "gfs-2")
    conns = await repo.list_gfs_for_space("sp-1")
    assert {c.id for c in conns} == {"gfs-1", "gfs-2"}


async def test_count_published_spaces(env):
    _, repo = env
    await repo.save(_make_conn("gfs-1"))
    assert await repo.count_published_spaces("gfs-1") == 0
    await repo.publish_space("sp-1", "gfs-1")
    await repo.publish_space("sp-2", "gfs-1")
    assert await repo.count_published_spaces("gfs-1") == 2


# ─── Service tests ──────────────────────────────────────────────────────


_OWN_PAIR_KW = {
    "own_instance_id": "alpha.home",
    "own_public_key_hex": "aa" * 32,
    "own_inbox_url": "https://alpha.example/federation/inbox",
    "own_display_name": "Alpha House",
}


async def test_pair_success(env):
    _, repo = env
    session = _StubSession(
        method_responses={
            "GET": (
                200,
                {
                    "gfs_instance_id": "remote-gfs-id",
                    "public_key": "bb" * 32,
                    "server_name": "Test GFS",
                    "base_url": "https://gfs.example.com",
                },
            ),
            "POST": (200, {"status": "registered", "instance_id": "alpha.home"}),
        },
    )
    svc = GfsConnectionService(repo, http_client=session)
    conn = await svc.pair(
        {"gfs_url": "https://gfs.example.com", "token": "tok-123"},
        **_OWN_PAIR_KW,
    )
    assert conn.status == "active"
    assert conn.gfs_instance_id == "remote-gfs-id"
    # The display name comes from the GFS's own ``server_name`` (its
    # branding), not anything the SH made up locally.
    assert conn.display_name == "Test GFS"
    # The pinned public key is the GFS's, fetched via ``/gfs/info`` —
    # this is the trust anchor for every subsequent relay.
    assert conn.public_key == "bb" * 32
    # Two calls: GET /gfs/info, then POST /gfs/register.
    assert session.calls == [
        ("GET", "https://gfs.example.com/gfs/info"),
        ("POST", "https://gfs.example.com/gfs/register"),
    ]
    # Saved to repo.
    saved = await repo.get(conn.id)
    assert saved is not None


async def test_pair_pending_status(env):
    """A GFS with auto-accept disabled returns ``status="pending"``;
    the local connection lands as ``pending`` (not ``active``) so the
    UI can render the "awaiting GFS admin review" state."""
    _, repo = env
    session = _StubSession(
        method_responses={
            "GET": (
                200,
                {
                    "gfs_instance_id": "remote",
                    "public_key": "cc" * 32,
                    "server_name": "GFS",
                    "base_url": "https://gfs",
                },
            ),
            "POST": (200, {"status": "pending"}),
        },
    )
    svc = GfsConnectionService(repo, http_client=session)
    conn = await svc.pair(
        {"gfs_url": "https://gfs", "token": "tok"},
        **_OWN_PAIR_KW,
    )
    assert conn.status == "pending"


async def test_pair_missing_qr_fields(env):
    _, repo = env
    svc = GfsConnectionService(repo, http_client=_StubSession())
    with pytest.raises(GfsConnectionError, match="QR payload"):
        await svc.pair({"gfs_url": "https://x.com"}, **_OWN_PAIR_KW)


async def test_pair_missing_own_identity(env):
    _, repo = env
    svc = GfsConnectionService(repo, http_client=_StubSession())
    with pytest.raises(GfsConnectionError, match="own_instance_id"):
        await svc.pair(
            {"gfs_url": "https://x.com", "token": "tok"},
            own_instance_id="",
            own_public_key_hex="ab",
            own_inbox_url="https://x",
        )


async def test_pair_gfs_info_unreachable(env):
    """A GFS that doesn't expose ``/gfs/info`` cannot be pinned —
    surface the failure cleanly instead of saving a half-trusted
    connection."""
    _, repo = env
    session = _StubSession(
        method_responses={"GET": (404, {})},
    )
    svc = GfsConnectionService(repo, http_client=session)
    with pytest.raises(GfsConnectionError, match="HTTP 404"):
        await svc.pair(
            {"gfs_url": "https://gfs.example.com", "token": "tok"},
            **_OWN_PAIR_KW,
        )


async def test_pair_register_rejects(env):
    _, repo = env
    session = _StubSession(
        method_responses={
            "GET": (
                200,
                {
                    "gfs_instance_id": "remote",
                    "public_key": "cc" * 32,
                    "server_name": "GFS",
                    "base_url": "https://gfs",
                },
            ),
            "POST": (401, {}),
        },
    )
    svc = GfsConnectionService(repo, http_client=session)
    with pytest.raises(GfsConnectionError, match="HTTP 401"):
        await svc.pair(
            {"gfs_url": "https://gfs.example.com", "token": "stale-tok"},
            **_OWN_PAIR_KW,
        )


async def test_pair_no_public_key_in_info(env):
    """``/gfs/info`` must return both ``gfs_instance_id`` and
    ``public_key`` — without the key there's no anchor to verify
    later reports against, so refuse to register."""
    _, repo = env
    session = _StubSession(
        method_responses={
            "GET": (200, {"gfs_instance_id": "remote", "public_key": ""}),
        },
    )
    svc = GfsConnectionService(repo, http_client=session)
    with pytest.raises(GfsConnectionError, match="gfs_instance_id and public_key"):
        await svc.pair(
            {"gfs_url": "https://gfs.example.com", "token": "tok"},
            **_OWN_PAIR_KW,
        )


async def test_disconnect_success(env):
    _, repo = env
    await repo.save(_make_conn("gfs-1"))
    svc = GfsConnectionService(repo, http_client=_StubSession())
    await svc.disconnect("gfs-1")
    assert await repo.get("gfs-1") is None


async def test_disconnect_not_found(env):
    _, repo = env
    svc = GfsConnectionService(repo, http_client=_StubSession())
    with pytest.raises(GfsConnectionError, match="not found"):
        await svc.disconnect("nonexistent")


async def test_list_connections(env):
    _, repo = env
    await repo.save(_make_conn("gfs-1"))
    await repo.save(_make_conn("gfs-2", status="suspended"))
    svc = GfsConnectionService(repo, http_client=_StubSession())
    result = await svc.list_connections()
    assert len(result) == 1
    assert result[0].id == "gfs-1"


async def test_publish_space_success(env):
    _, repo = env
    await repo.save(_make_conn("gfs-1"))
    session = _StubSession(status=204)
    svc = GfsConnectionService(repo, http_client=session)
    await svc.publish_space("sp-1", "gfs-1")
    pubs = await repo.list_publications("gfs-1")
    assert len(pubs) == 1
    assert pubs[0].space_id == "sp-1"
    assert len(session.calls) == 1


async def test_publish_space_not_found(env):
    _, repo = env
    svc = GfsConnectionService(repo, http_client=_StubSession())
    with pytest.raises(GfsConnectionError, match="not found"):
        await svc.publish_space("sp-1", "nonexistent")


async def test_unpublish_space_success(env):
    _, repo = env
    await repo.save(_make_conn("gfs-1"))
    await repo.publish_space("sp-1", "gfs-1")
    session = _StubSession(status=204)
    svc = GfsConnectionService(repo, http_client=session)
    await svc.unpublish_space("sp-1", "gfs-1")
    pubs = await repo.list_publications("gfs-1")
    assert pubs == []


async def test_unpublish_space_not_found(env):
    _, repo = env
    svc = GfsConnectionService(repo, http_client=_StubSession())
    with pytest.raises(GfsConnectionError, match="not found"):
        await svc.unpublish_space("sp-1", "nonexistent")


# ─── Sync-signaling round-robin (spec §24.10.7) ───────────────────────


async def test_request_signaling_node_returns_url(env):
    _, repo = env
    await repo.save(_make_conn("gfs-1"))
    kp = generate_identity_keypair()
    session = _StubSession(
        status=200,
        body={"signaling_node": "https://b.gfs.test", "session_id": "s1"},
    )
    svc = GfsConnectionService(repo, http_client=session)
    result = await svc.request_signaling_node(
        "s1",
        from_instance="caller.home",
        signing_key=kp.private_key,
    )
    assert result == "https://b.gfs.test"
    # Posted to the right URL with a signature attached.
    assert session.calls == [
        ("POST", "https://gfs.example.com/cluster/signaling-session")
    ]


async def test_request_signaling_node_503_returns_none(env):
    _, repo = env
    await repo.save(_make_conn("gfs-1"))
    kp = generate_identity_keypair()
    session = _StubSession(status=503, body={"reason": "node_capacity"})
    svc = GfsConnectionService(repo, http_client=session)
    result = await svc.request_signaling_node(
        "s1",
        from_instance="caller.home",
        signing_key=kp.private_key,
    )
    assert result is None


async def test_request_signaling_node_null_returns_none(env):
    """Single-node GFS: ``signaling_node: null`` → None."""
    _, repo = env
    await repo.save(_make_conn("gfs-1"))
    kp = generate_identity_keypair()
    session = _StubSession(status=200, body={"signaling_node": None})
    svc = GfsConnectionService(repo, http_client=session)
    result = await svc.request_signaling_node(
        "s1",
        from_instance="caller.home",
        signing_key=kp.private_key,
    )
    assert result is None


async def test_request_signaling_node_no_active_gfs_returns_none(env):
    """No paired active GFS → None (HFS-only deployment)."""
    _, repo = env
    kp = generate_identity_keypair()
    svc = GfsConnectionService(repo, http_client=_StubSession())
    result = await svc.request_signaling_node(
        "s1",
        from_instance="caller.home",
        signing_key=kp.private_key,
    )
    assert result is None


async def test_release_signaling_node_posts(env):
    _, repo = env
    await repo.save(_make_conn("gfs-1"))
    kp = generate_identity_keypair()
    session = _StubSession(status=200, body={"status": "released"})
    svc = GfsConnectionService(repo, http_client=session)
    await svc.release_signaling_node(
        "s1",
        "https://b.gfs.test",
        from_instance="caller.home",
        signing_key=kp.private_key,
    )
    assert session.calls == [
        ("POST", "https://gfs.example.com/cluster/signaling-session/release"),
    ]


async def test_release_signaling_node_no_url_is_noop(env):
    _, repo = env
    await repo.save(_make_conn("gfs-1"))
    kp = generate_identity_keypair()
    session = _StubSession(status=200)
    svc = GfsConnectionService(repo, http_client=session)
    await svc.release_signaling_node(
        "s1",
        "",
        from_instance="caller.home",
        signing_key=kp.private_key,
    )
    assert session.calls == []


# ── publish-space context (attach_publish_context + _build_publish_body) ──


async def test_publish_body_falls_back_when_context_unset(env):
    """Without ``attach_publish_context``, ``publish_space`` ships only
    the bare ``{space_id}`` body — preserves the legacy shape so an
    unmigrated SH (no identity wired) still triggers a ``status='pending'``
    row on the GFS rather than failing the publish call entirely."""
    _, repo = env
    await repo.save(_make_conn("gfs-1", inbox_url="https://gfs.example"))
    session = _StubSession(method_responses={"POST": (200, {"status": "pending"})})
    svc = GfsConnectionService(repo, http_client=session)
    # No attach_publish_context call. publish_space still works.
    await svc.publish_space("sp-bare", "gfs-1")
    assert session.calls == [("POST", "https://gfs.example/gfs/spaces/sp-bare/publish")]


async def test_publish_body_carries_metadata_and_signature(env):
    """With ``attach_publish_context`` wired, the publish body includes
    the local space's name + description + signed canonical JSON the
    GFS verifies against ``ClientInstance.public_key``."""
    from socialhome.crypto import (
        b64url_decode,
        verify_ed25519,
    )
    from socialhome.domain.space import (
        JoinMode,
        Space,
        SpaceFeatures,
        SpaceType,
    )
    from socialhome.repositories.space_repo import SqliteSpaceRepo

    db, conn_repo = env
    await conn_repo.save(_make_conn("gfs-2", inbox_url="https://gfs.example"))
    space_repo = SqliteSpaceRepo(db)
    space = Space(
        id="sp-rich",
        name="Local Birds",
        owner_instance_id="alpha.home",
        owner_username="alice",
        identity_public_key="aa" * 32,
        config_sequence=0,
        features=SpaceFeatures(),
        space_type=SpaceType.GLOBAL,
        join_mode=JoinMode.OPEN,
        description="everyday birds in the neighbourhood",
    )
    await space_repo.save(space)

    kp = generate_identity_keypair()
    session = _StubSession(
        method_responses={"POST": (200, {"status": "registered"})},
    )
    svc = GfsConnectionService(conn_repo, http_client=session)
    svc.attach_publish_context(
        space_repo=space_repo,
        own_instance_id="alpha.home",
        own_signing_key=kp.private_key,
    )
    await svc.publish_space("sp-rich", "gfs-2")
    # Capture the body the stub session forwarded.
    body = session._last_body  # type: ignore[attr-defined]
    assert body["space_id"] == "sp-rich"
    assert body["owning_instance"] == "alpha.home"
    assert body["name"] == "Local Birds"
    assert body["description"] == "everyday birds in the neighbourhood"
    sig_b64 = body.pop("signature")
    canonical = json.dumps(body, separators=(",", ":"), sort_keys=True).encode("utf-8")
    assert verify_ed25519(kp.public_key, canonical, b64url_decode(sig_b64))


async def test_publish_body_falls_back_when_space_missing(env):
    """``attach_publish_context`` is wired but the local space row is
    gone — fall back to the legacy ``{space_id}`` body rather than
    crashing the auto-publish hook. The GFS will still create a
    pending row keyed on the id; an admin or a re-publish can fill
    in the metadata later."""
    from socialhome.repositories.space_repo import SqliteSpaceRepo

    db, conn_repo = env
    await conn_repo.save(_make_conn("gfs-3", inbox_url="https://gfs.example"))
    kp = generate_identity_keypair()
    session = _StubSession(method_responses={"POST": (200, {"status": "pending"})})
    svc = GfsConnectionService(conn_repo, http_client=session)
    svc.attach_publish_context(
        space_repo=SqliteSpaceRepo(db),
        own_instance_id="alpha.home",
        own_signing_key=kp.private_key,
    )
    await svc.publish_space("sp-missing", "gfs-3")
    # We don't have the body-capture in the legacy path; just assert
    # the request landed and didn't raise.
    assert session.calls == [
        ("POST", "https://gfs.example/gfs/spaces/sp-missing/publish"),
    ]
