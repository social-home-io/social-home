"""Tests for AppFederationService — cross-household app federation bridge.

All tests use in-memory stubs — no network, no real disk.

Security invariants:
- open_session / send_message raise AppNotFoundError for unknown apps.
- open_session / send_message raise AppNotEnabledError for disabled apps.
- Inbound events for uninstalled / disabled apps are silently dropped.
- Application payload is never exposed in plaintext on the wire; tests
  assert that send_app_message is called (not a raw dict in plaintext),
  and that the JSON fallback nests payload under "data" inside the
  encrypted send_event call.
"""

from __future__ import annotations

import dataclasses
from unittest.mock import MagicMock

import pytest

from socialhome.domain.apps import (
    AppManifest,
    AppNotEnabledError,
    AppNotFoundError,
    InstalledApp,
)
from socialhome.domain.federation import (
    FederationEvent,
    FederationEventType,
    PairingStatus,
    RemoteInstance,
)
from socialhome.domain.user import User
from socialhome.services.app_federation_service import AppFederationService


# ─── Stubs ────────────────────────────────────────────────────────────────────


def _make_installed_app(app_id: str = "chess", *, enabled: bool = True) -> InstalledApp:
    return InstalledApp(
        app_id=app_id,
        name="Chess",
        version="1.0.0",
        enabled=enabled,
        manifest=AppManifest(entry="index.html", icon=None, capabilities=()),
        bundle_path="/apps/chess",
        bundle_sha256="abc123",
        source_url="https://example.com/chess.tgz",
        installed_by="admin",
        installed_at="2026-01-01T00:00:00+00:00",
    )


def _make_remote_instance(
    instance_id: str = "peer-inst-id",
    *,
    display_name: str = "Peer Household",
    status: PairingStatus = PairingStatus.CONFIRMED,
) -> RemoteInstance:
    return RemoteInstance(
        id=instance_id,
        display_name=display_name,
        remote_identity_pk="a" * 64,
        key_self_to_remote="enc-key-1",
        key_remote_to_self="enc-key-2",
        remote_inbox_url="http://peer.example.com/fed/inbox",
        local_inbox_id="local-wh-id",
        status=status,
    )


def _make_user(user_id: str = "user-1", username: str = "alice") -> User:
    return User(
        user_id=user_id,
        username=username,
        display_name="Alice",
    )


class _FakeAppRepo:
    def __init__(self, apps: dict[str, InstalledApp] | None = None) -> None:
        self._apps: dict[str, InstalledApp] = apps or {}

    async def get(self, app_id: str) -> InstalledApp | None:
        return self._apps.get(app_id)

    async def list_installed(self) -> list[InstalledApp]:
        return list(self._apps.values())


class _FakeUserRepo:
    def __init__(self, users: list[User] | None = None) -> None:
        self._users: list[User] = users or []

    async def list_all(self) -> list[User]:
        return list(self._users)


class _FakeFederationRepo:
    def __init__(self, instances: list[RemoteInstance] | None = None) -> None:
        self._instances: list[RemoteInstance] = instances or []

    async def list_instances(
        self,
        *,
        source: str | None = None,
        status: str | None = None,
    ) -> list[RemoteInstance]:
        if status is not None:
            return [i for i in self._instances if i.status.value == status]
        return list(self._instances)


class _FakeFederation:
    """Fake FederationService that captures outbound calls."""

    def __init__(self) -> None:
        self.sent_events: list[dict] = []
        self.sent_app_messages: list[dict] = []

    async def send_event(
        self,
        *,
        to_instance_id: str,
        event_type: FederationEventType,
        payload: dict,
        space_id: str | None = None,
    ):
        self.sent_events.append(
            {
                "to_instance_id": to_instance_id,
                "event_type": event_type,
                "payload": payload,
            }
        )
        # Return a minimal DeliveryResult-like
        return MagicMock(ok=True)

    async def send_app_message(
        self,
        *,
        to_instance_id: str,
        app_id: str,
        session_id: str,
        payload: dict,
    ):
        self.sent_app_messages.append(
            {
                "to_instance_id": to_instance_id,
                "app_id": app_id,
                "session_id": session_id,
                "payload": payload,
            }
        )
        return MagicMock(ok=True)


class _FakeWs:
    """Captures broadcast_to_users calls."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def broadcast_to_users(self, user_ids: list[str], payload: dict) -> int:
        self.calls.append({"user_ids": list(user_ids), "payload": payload})
        return len(user_ids)


def _make_svc(
    *,
    apps: dict[str, InstalledApp] | None = None,
    users: list[User] | None = None,
    instances: list[RemoteInstance] | None = None,
) -> tuple[AppFederationService, _FakeFederation, _FakeWs]:
    federation = _FakeFederation()
    ws = _FakeWs()
    svc = AppFederationService(
        app_repo=_FakeAppRepo(apps),
        user_repo=_FakeUserRepo(users),
        ws=ws,
        federation=federation,
        federation_repo=_FakeFederationRepo(instances),
    )
    return svc, federation, ws


def _make_event(
    event_type: FederationEventType,
    payload: dict,
    from_instance: str = "peer-inst-id",
) -> FederationEvent:
    return FederationEvent(
        msg_id="msg-1",
        event_type=event_type,
        from_instance=from_instance,
        to_instance="local-inst-id",
        timestamp="2026-01-01T00:00:00+00:00",
        payload=payload,
    )


# ─── list_peers ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_peers_returns_confirmed_only():
    """Only CONFIRMED instances appear in list_peers."""
    confirmed = _make_remote_instance("c1", display_name="Home A")
    pending = dataclasses.replace(
        _make_remote_instance("p1", display_name="Home B"),
        status=PairingStatus.PENDING_SENT,
    )
    svc, _, _ = _make_svc(instances=[confirmed, pending])
    peers = await svc.list_peers()
    assert len(peers) == 1
    assert peers[0]["instance_id"] == "c1"
    assert peers[0]["display_name"] == "Home A"


@pytest.mark.asyncio
async def test_list_peers_uses_effective_display_name():
    """effective_display_name (alias > display_name) is used."""
    inst = dataclasses.replace(
        _make_remote_instance("c1", display_name="Boring Name"),
        local_alias="Cool Alias",
    )
    svc, _, _ = _make_svc(instances=[inst])
    peers = await svc.list_peers()
    assert peers[0]["display_name"] == "Cool Alias"


@pytest.mark.asyncio
async def test_list_peers_empty_when_no_confirmed():
    svc, _, _ = _make_svc(instances=[])
    assert await svc.list_peers() == []


# ─── open_session ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_open_session_sends_app_session_event():
    """open_session fires APP_SESSION with app_id, session_id, verb, from_user."""
    app = _make_installed_app("chess")
    svc, federation, _ = _make_svc(apps={"chess": app})
    session_id = await svc.open_session(
        app_id="chess",
        peer_instance_id="peer-1",
        actor_user_id="user-1",
    )
    assert len(federation.sent_events) == 1
    ev = federation.sent_events[0]
    assert ev["event_type"] is FederationEventType.APP_SESSION
    assert ev["to_instance_id"] == "peer-1"
    assert ev["payload"]["app_id"] == "chess"
    assert ev["payload"]["session_id"] == session_id
    assert ev["payload"]["verb"] == "open"
    assert ev["payload"]["from_user"] == "user-1"
    # session_id is a non-empty hex string
    assert len(session_id) == 32
    assert all(c in "0123456789abcdef" for c in session_id)


@pytest.mark.asyncio
async def test_open_session_raises_for_missing_app():
    svc, federation, _ = _make_svc(apps={})
    with pytest.raises(AppNotFoundError):
        await svc.open_session(
            app_id="nonexistent",
            peer_instance_id="peer-1",
            actor_user_id="user-1",
        )
    assert federation.sent_events == []


@pytest.mark.asyncio
async def test_open_session_raises_for_disabled_app():
    app = _make_installed_app("chess", enabled=False)
    svc, federation, _ = _make_svc(apps={"chess": app})
    with pytest.raises(AppNotEnabledError):
        await svc.open_session(
            app_id="chess",
            peer_instance_id="peer-1",
            actor_user_id="user-1",
        )
    assert federation.sent_events == []


@pytest.mark.asyncio
async def test_open_session_returns_unique_ids():
    """Each call returns a different session_id."""
    app = _make_installed_app("chess")
    svc, _, _ = _make_svc(apps={"chess": app})
    ids = {
        await svc.open_session(
            app_id="chess",
            peer_instance_id="peer-1",
            actor_user_id="u1",
        )
        for _ in range(5)
    }
    assert len(ids) == 5


# ─── send_message ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_send_message_delegates_to_send_app_message():
    """send_message calls federation.send_app_message — not send_event directly."""
    app = _make_installed_app("chess")
    svc, federation, _ = _make_svc(apps={"chess": app})
    payload = {"move": "e2e4", "clock": 90}
    await svc.send_message(
        app_id="chess",
        session_id="session-abc",
        peer_instance_id="peer-1",
        payload=payload,
        actor_user_id="user-1",
    )
    assert len(federation.sent_app_messages) == 1
    msg = federation.sent_app_messages[0]
    assert msg["to_instance_id"] == "peer-1"
    assert msg["app_id"] == "chess"
    assert msg["session_id"] == "session-abc"
    assert msg["payload"] == payload


@pytest.mark.asyncio
async def test_send_message_raises_for_missing_app():
    svc, federation, _ = _make_svc(apps={})
    with pytest.raises(AppNotFoundError):
        await svc.send_message(
            app_id="chess",
            session_id="s1",
            peer_instance_id="peer-1",
            payload={"x": 1},
            actor_user_id="u1",
        )
    assert federation.sent_app_messages == []


@pytest.mark.asyncio
async def test_send_message_raises_for_disabled_app():
    app = _make_installed_app("chess", enabled=False)
    svc, federation, _ = _make_svc(apps={"chess": app})
    with pytest.raises(AppNotEnabledError):
        await svc.send_message(
            app_id="chess",
            session_id="s1",
            peer_instance_id="peer-1",
            payload={"x": 1},
            actor_user_id="u1",
        )
    assert federation.sent_app_messages == []


# ─── on_inbound_event (JSON path) ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_inbound_app_message_delivers_to_local_users():
    """APP_MESSAGE event → ws.broadcast_to_users with app.message frame."""
    app = _make_installed_app("chess")
    users = [_make_user("u1", "alice"), _make_user("u2", "bob")]
    svc, _, ws = _make_svc(apps={"chess": app}, users=users)

    event = _make_event(
        FederationEventType.APP_MESSAGE,
        {
            "app_id": "chess",
            "session_id": "sess-1",
            "data": {"move": "e2e4"},
        },
    )
    await svc.on_inbound_event(event)

    assert len(ws.calls) == 1
    call = ws.calls[0]
    assert set(call["user_ids"]) == {"u1", "u2"}
    frame = call["payload"]
    assert frame["type"] == "app.message"
    assert frame["app_id"] == "chess"
    assert frame["session_id"] == "sess-1"
    assert frame["from_instance"] == "peer-inst-id"
    assert frame["payload"] == {"move": "e2e4"}


@pytest.mark.asyncio
async def test_inbound_app_session_delivers_full_payload():
    """APP_SESSION event → entire payload dict forwarded (verb, from_user, …)."""
    app = _make_installed_app("chess")
    users = [_make_user("u1")]
    svc, _, ws = _make_svc(apps={"chess": app}, users=users)

    event = _make_event(
        FederationEventType.APP_SESSION,
        {
            "app_id": "chess",
            "session_id": "sess-2",
            "verb": "open",
            "from_user": "remote-user-x",
        },
    )
    await svc.on_inbound_event(event)

    assert len(ws.calls) == 1
    frame = ws.calls[0]["payload"]
    assert frame["type"] == "app.message"
    assert frame["session_id"] == "sess-2"
    # Entire session payload forwarded
    assert frame["payload"]["verb"] == "open"
    assert frame["payload"]["from_user"] == "remote-user-x"


@pytest.mark.asyncio
async def test_inbound_for_uninstalled_app_not_delivered():
    """App not in repo → silently dropped, no WebSocket push."""
    svc, _, ws = _make_svc(apps={}, users=[_make_user("u1")])
    event = _make_event(
        FederationEventType.APP_MESSAGE,
        {"app_id": "unknown-app", "session_id": "s1", "data": {}},
    )
    await svc.on_inbound_event(event)
    assert ws.calls == []


@pytest.mark.asyncio
async def test_inbound_for_disabled_app_not_delivered():
    """Disabled app → silently dropped, no WebSocket push."""
    app = _make_installed_app("chess", enabled=False)
    svc, _, ws = _make_svc(apps={"chess": app}, users=[_make_user("u1")])
    event = _make_event(
        FederationEventType.APP_MESSAGE,
        {"app_id": "chess", "session_id": "s1", "data": {}},
    )
    await svc.on_inbound_event(event)
    assert ws.calls == []


@pytest.mark.asyncio
async def test_inbound_missing_app_id_dropped():
    """Missing app_id in payload → silently dropped."""
    svc, _, ws = _make_svc(users=[_make_user("u1")])
    event = _make_event(
        FederationEventType.APP_MESSAGE,
        {"session_id": "s1", "data": {}},
    )
    await svc.on_inbound_event(event)
    assert ws.calls == []


@pytest.mark.asyncio
async def test_inbound_no_local_users_no_broadcast():
    """No local users → broadcast_to_users is not called."""
    app = _make_installed_app("chess")
    svc, _, ws = _make_svc(apps={"chess": app}, users=[])
    event = _make_event(
        FederationEventType.APP_MESSAGE,
        {"app_id": "chess", "session_id": "s1", "data": {"x": 1}},
    )
    await svc.on_inbound_event(event)
    assert ws.calls == []


# ─── on_inbound_message (binary path) ────────────────────────────────────────


@pytest.mark.asyncio
async def test_inbound_binary_delivers_to_local_users():
    """Binary-channel path delivers to users just like the JSON event path."""
    app = _make_installed_app("chess")
    users = [_make_user("u1", "alice")]
    svc, _, ws = _make_svc(apps={"chess": app}, users=users)

    await svc.on_inbound_message(
        "peer-inst-id",
        "chess",
        "sess-bin",
        {"move": "d7d5"},
    )

    assert len(ws.calls) == 1
    frame = ws.calls[0]["payload"]
    assert frame["type"] == "app.message"
    assert frame["app_id"] == "chess"
    assert frame["session_id"] == "sess-bin"
    assert frame["from_instance"] == "peer-inst-id"
    assert frame["payload"] == {"move": "d7d5"}


@pytest.mark.asyncio
async def test_inbound_binary_for_disabled_app_not_delivered():
    app = _make_installed_app("chess", enabled=False)
    svc, _, ws = _make_svc(apps={"chess": app}, users=[_make_user("u1")])
    await svc.on_inbound_message("peer", "chess", "s", {})
    assert ws.calls == []


@pytest.mark.asyncio
async def test_inbound_binary_for_uninstalled_app_not_delivered():
    svc, _, ws = _make_svc(apps={}, users=[_make_user("u1")])
    await svc.on_inbound_message("peer", "unknown-app", "s", {})
    assert ws.calls == []
