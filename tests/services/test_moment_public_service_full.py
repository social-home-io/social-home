"""Full HTTP-mocked tests for :class:`MomentPublicService`.

Mirrors the shape of ``test_highlight_publication_service.py``:
stubs the aiohttp session so the sign-then-POST pattern is
exercised end-to-end without a real socket, and asserts the local
state transitions on every flow (register / deregister / follow /
unfollow / set_default_share / fetch_directory).
"""

from __future__ import annotations

import pytest

from socialhome.repositories.gfs_connection_repo import SqliteGfsConnectionRepo
from socialhome.repositories.moment_public_repo import (
    SqliteMomentPublicFollowRepo,
    SqliteMomentPublicRegistrationRepo,
)
from socialhome.repositories.user_repo import SqliteUserRepo
from socialhome.services.moment_public_service import (
    MomentPublicError,
    MomentPublicService,
)


class _StubResp:
    def __init__(self, status: int = 201, body: dict | None = None) -> None:
        self.status = status
        self._body = body or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def json(self):
        return self._body


class _StubSession:
    """Captures every call so tests can assert URL + body shape."""

    def __init__(self, *, status: int = 201, body: dict | None = None) -> None:
        self.status = status
        self.body = body or {}
        self.posts: list[tuple[str, dict]] = []
        self.gets: list[str] = []

    def post(self, url, *, json=None, **_kw):
        self.posts.append((url, json or {}))
        return _StubResp(self.status, self.body)

    def get(self, url, **_kw):
        self.gets.append(url)
        return _StubResp(self.status, self.body)


@pytest.fixture
async def repos(db):
    await db.enqueue(
        "INSERT INTO users(user_id, username, display_name, state) "
        "VALUES('u1','alice','Alice','active')"
    )
    await db.enqueue(
        "INSERT INTO gfs_connections("
        "id, gfs_instance_id, display_name, public_key, inbox_url, "
        "status, paired_at) VALUES('g1','gfs-1','GFS One','ff'*32,"
        "'https://gfs1.example','active', datetime('now'))"
    )
    return {
        "regs": SqliteMomentPublicRegistrationRepo(db),
        "follows": SqliteMomentPublicFollowRepo(db),
        "users": SqliteUserRepo(db),
        "gfs": SqliteGfsConnectionRepo(db),
    }


def _make_service(repos, *, session):
    svc = MomentPublicService(
        repos["regs"], repos["follows"], repos["users"], repos["gfs"]
    )
    svc.attach_session(session)
    svc.attach_identity(own_instance_id="inst-self", signing_key=b"\x01" * 32)
    return svc


async def test_register_signs_body_and_persists_local_row(repos):
    sess = _StubSession(body={"user_id": "u1"})
    svc = _make_service(repos, session=sess)
    reg = await svc.register(user_id="u1", gfs_id="g1", default_share=True)
    assert reg.user_id == "u1" and reg.gfs_id == "g1"
    assert sess.posts and sess.posts[0][0].endswith("/gfs/users/register")
    body = sess.posts[0][1]
    assert body["user_id"] == "u1"
    assert body["instance_id"] == "inst-self"
    assert body["username"] == "alice"
    assert body["display_name"] == "Alice"
    assert body["home_instance_pk"]  # 64 hex chars
    assert "signature" in body
    saved = await repos["regs"].get(user_id="u1", gfs_id="g1")
    assert saved is not None and saved.default_share is True


async def test_register_unknown_user_raises_lookup(repos):
    svc = _make_service(repos, session=_StubSession())
    with pytest.raises(LookupError):
        await svc.register(user_id="nobody", gfs_id="g1")


async def test_register_unknown_gfs_raises(repos):
    svc = _make_service(repos, session=_StubSession())
    with pytest.raises(MomentPublicError):
        await svc.register(user_id="u1", gfs_id="missing-gfs")


async def test_register_propagates_gfs_500(repos):
    svc = _make_service(repos, session=_StubSession(status=500))
    with pytest.raises(MomentPublicError):
        await svc.register(user_id="u1", gfs_id="g1")


async def test_set_default_share_then_is_registered(repos):
    svc = _make_service(repos, session=_StubSession(body={}))
    await svc.register(user_id="u1", gfs_id="g1", default_share=True)
    assert await svc.is_registered(user_id="u1", gfs_id="g1") is True
    assert await svc.default_share(user_id="u1", gfs_id="g1") is True
    await svc.set_default_share(user_id="u1", gfs_id="g1", default_share=False)
    assert await svc.default_share(user_id="u1", gfs_id="g1") is False


async def test_deregister_clears_local_even_when_gfs_returns_404(repos):
    svc = _make_service(repos, session=_StubSession(body={}))
    await svc.register(user_id="u1", gfs_id="g1")
    sess_404 = _StubSession(status=404, body={})
    svc._http_client = sess_404  # type: ignore[assignment]
    await svc.deregister(user_id="u1", gfs_id="g1")
    assert await svc.is_registered(user_id="u1", gfs_id="g1") is False
    # The deregister still POSTs (so the GFS can clean up) before the
    # local row drops.
    assert sess_404.posts and sess_404.posts[0][0].endswith("/gfs/users/u1/deregister")


async def test_list_registrations_round_trip(repos):
    svc = _make_service(repos, session=_StubSession(body={}))
    await svc.register(user_id="u1", gfs_id="g1")
    rows = await svc.list_registrations("u1")
    assert len(rows) == 1 and rows[0].gfs_id == "g1"


async def test_follow_caches_directory_entry_locally(repos):
    sess = _StubSession(
        body={
            "user": {
                "user_id": "u-remote",
                "username": "bob",
                "display_name": "Bob",
                "picture_url": None,
                "home_instance_pk": "ab" * 32,
                "instance_id": "inst-remote",
            },
            "follow": {
                "follower_user_id": "u1",
                "followed_user_id": "u-remote",
                "created_at": 1,
            },
        }
    )
    svc = _make_service(repos, session=sess)
    f = await svc.follow(
        follower_user_id="u1", gfs_id="g1", followed_user_id="u-remote"
    )
    assert f.followed_user_id == "u-remote"
    assert f.followed_instance_pk == "ab" * 32
    assert f.followed_username == "bob"
    assert f.followed_display_name == "Bob"
    body = sess.posts[0][1]
    assert body["follower_user_id"] == "u1"
    assert body["follower_instance_id"] == "inst-self"
    assert "signature" in body


async def test_follow_propagates_gfs_failure(repos):
    sess = _StubSession(status=500, body={"error": "boom"})
    svc = _make_service(repos, session=sess)
    with pytest.raises(MomentPublicError):
        await svc.follow(
            follower_user_id="u1", gfs_id="g1", followed_user_id="u-remote"
        )


async def test_unfollow_drops_local_row(repos):
    # Seed a follow row first.
    await repos["follows"].upsert(
        follower_user_id="u1",
        followed_user_id="u-remote",
        gfs_id="g1",
        followed_instance_pk="ab" * 32,
        followed_username="bob",
        followed_display_name="Bob",
    )
    svc = _make_service(repos, session=_StubSession(body={}))
    await svc.unfollow(follower_user_id="u1", gfs_id="g1", followed_user_id="u-remote")
    rows = await repos["follows"].list_for_follower("u1")
    assert rows == []


async def test_list_follows_round_trip(repos):
    await repos["follows"].upsert(
        follower_user_id="u1",
        followed_user_id="u-remote",
        gfs_id="g1",
        followed_instance_pk="ab" * 32,
        followed_username="bob",
        followed_display_name="Bob",
    )
    svc = _make_service(repos, session=_StubSession())
    rows = await svc.list_follows("u1")
    assert len(rows) == 1 and rows[0].followed_username == "bob"


async def test_fetch_directory_returns_users_list(repos):
    svc = _make_service(
        repos,
        session=_StubSession(
            status=200,
            body={
                "users": [
                    {"user_id": "u-remote", "display_name": "Bob"},
                ]
            },
        ),
    )
    users = await svc.fetch_directory("g1")
    assert users == [{"user_id": "u-remote", "display_name": "Bob"}]


async def test_fetch_directory_5xx_raises(repos):
    svc = _make_service(repos, session=_StubSession(status=500, body={}))
    with pytest.raises(MomentPublicError):
        await svc.fetch_directory("g1")


async def test_service_used_before_attach_raises(repos):
    svc = MomentPublicService(
        repos["regs"], repos["follows"], repos["users"], repos["gfs"]
    )
    # No attach_session/attach_identity → calls should fail noisily.
    with pytest.raises(MomentPublicError):
        await svc.register(user_id="u1", gfs_id="g1")


# ── ``aiohttp.ClientError`` paths ───────────────────────────────────────


class _RaisingSession:
    """Session whose POSTs raise ``aiohttp.ClientError`` synchronously."""

    def __init__(self) -> None:
        self.posts: list[str] = []

    def post(self, url, *, json=None, **_kw):
        import aiohttp

        self.posts.append(url)
        raise aiohttp.ClientError("connection refused")

    def get(self, url, **_kw):
        import aiohttp

        raise aiohttp.ClientError("connection refused")


async def test_register_raises_on_client_error(repos):
    svc = MomentPublicService(
        repos["regs"], repos["follows"], repos["users"], repos["gfs"]
    )
    svc.attach_session(_RaisingSession())  # type: ignore[arg-type]
    svc.attach_identity(own_instance_id="inst-self", signing_key=b"\x01" * 32)
    with pytest.raises(MomentPublicError):
        await svc.register(user_id="u1", gfs_id="g1")


async def test_follow_raises_on_client_error(repos):
    svc = MomentPublicService(
        repos["regs"], repos["follows"], repos["users"], repos["gfs"]
    )
    svc.attach_session(_RaisingSession())  # type: ignore[arg-type]
    svc.attach_identity(own_instance_id="inst-self", signing_key=b"\x01" * 32)
    with pytest.raises(MomentPublicError):
        await svc.follow(
            follower_user_id="u1", gfs_id="g1", followed_user_id="u-remote"
        )


async def test_fetch_directory_raises_on_client_error(repos):
    svc = MomentPublicService(
        repos["regs"], repos["follows"], repos["users"], repos["gfs"]
    )
    svc.attach_session(_RaisingSession())  # type: ignore[arg-type]
    svc.attach_identity(own_instance_id="inst-self", signing_key=b"\x01" * 32)
    with pytest.raises(MomentPublicError):
        await svc.fetch_directory("g1")


async def test_deregister_swallows_client_error_but_clears_local(repos):
    svc = MomentPublicService(
        repos["regs"], repos["follows"], repos["users"], repos["gfs"]
    )
    # Register first via a successful session.
    svc.attach_session(_StubSession(body={}))
    svc.attach_identity(own_instance_id="inst-self", signing_key=b"\x01" * 32)
    await svc.register(user_id="u1", gfs_id="g1")
    # Now swap in a session that raises and call deregister.
    svc._http_client = _RaisingSession()  # type: ignore[assignment]
    await svc.deregister(user_id="u1", gfs_id="g1")
    # Local row dropped despite the GFS being unreachable.
    assert await svc.is_registered(user_id="u1", gfs_id="g1") is False


async def test_unfollow_swallows_client_error_but_clears_local(repos):
    # Seed a follow row.
    await repos["follows"].upsert(
        follower_user_id="u1",
        followed_user_id="u-remote",
        gfs_id="g1",
        followed_instance_pk="ab" * 32,
        followed_username="bob",
        followed_display_name="Bob",
    )
    svc = MomentPublicService(
        repos["regs"], repos["follows"], repos["users"], repos["gfs"]
    )
    svc.attach_session(_RaisingSession())  # type: ignore[arg-type]
    svc.attach_identity(own_instance_id="inst-self", signing_key=b"\x01" * 32)
    await svc.unfollow(follower_user_id="u1", gfs_id="g1", followed_user_id="u-remote")
    rows = await repos["follows"].list_for_follower("u1")
    assert rows == []
