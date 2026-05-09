"""Cross-household DM coverage for :class:`DmService`.

Specifically exercises the ``other_user_id`` path on
:meth:`DmService.create_dm` (resolving a remote target via
``user_repo.get_remote``) and the corresponding fan-out on
:meth:`DmService.send_message` (resolving the remote member's user_id
via ``user_repo.list_remote_for_instance`` so the federation
envelope's ``recipient_user_ids`` carries the peer-side user the
recipient should be seated against).
"""

from __future__ import annotations

import pytest

from socialhome.crypto import derive_instance_id, generate_identity_keypair
from socialhome.db.database import AsyncDatabase
from socialhome.domain.conversation import ConversationType
from socialhome.domain.federation import (
    FederationEventType,
    PairingStatus,
)
from socialhome.domain.user import RemoteUser
from socialhome.infrastructure.event_bus import EventBus
from socialhome.repositories.conversation_repo import SqliteConversationRepo
from socialhome.repositories.user_repo import SqliteUserRepo
from socialhome.services.dm_service import DmService
from socialhome.services.user_service import UserService


@pytest.fixture
async def stack(tmp_dir):
    kp = generate_identity_keypair()
    iid = derive_instance_id(kp.public_key)
    db = AsyncDatabase(tmp_dir / "test.db", batch_timeout_ms=10)
    await db.startup()
    await db.enqueue(
        """INSERT INTO instance_identity(instance_id, identity_private_key,
           identity_public_key, routing_secret) VALUES(?,?,?,?)""",
        (iid, kp.private_key.hex(), kp.public_key.hex(), "aa" * 32),
    )
    bus = EventBus()
    user_repo = SqliteUserRepo(db)
    conv_repo = SqliteConversationRepo(db)
    user_svc = UserService(user_repo, bus, own_instance_public_key=kp.public_key)
    dm_svc = DmService(conv_repo, user_repo, bus)

    class Stack:
        pass

    s = Stack()
    s.db = db
    s.user_repo = user_repo
    s.conv_repo = conv_repo
    s.user_svc = user_svc
    s.dm_svc = dm_svc
    s.own_instance_id = iid

    async def provision(username: str):
        return await user_svc.provision(username=username, display_name=username)

    s.provision = provision
    yield s
    await db.shutdown()


class _FakeFederationService:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_event(self, *, to_instance_id, event_type, payload):
        self.sent.append(
            {"to": to_instance_id, "type": event_type, "payload": payload},
        )


class _FakeFederationRepo:
    def __init__(self, instances: dict[str, object]) -> None:
        self._instances = instances

    async def get_instance(self, instance_id: str):
        return self._instances.get(instance_id)


def _confirmed_peer(instance_id: str):
    from types import SimpleNamespace

    return SimpleNamespace(id=instance_id, status=PairingStatus.CONFIRMED)


async def _seed_remote_instance(stack, instance_id: str) -> None:
    """Insert a minimal ``remote_instances`` row so the FK on
    ``remote_users.instance_id`` is satisfied.
    """
    await stack.db.enqueue(
        """INSERT OR IGNORE INTO remote_instances(
               id, display_name, remote_identity_pk, key_self_to_remote,
               key_remote_to_self, remote_inbox_url, local_inbox_id,
               status, source
           ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            instance_id,
            instance_id[:8],
            "00" * 32,
            "k1",
            "k2",
            "https://peer.example/federation/inbox/x",
            "local-inbox",
            "confirmed",
            "manual",
        ),
    )


async def _seed_remote_user(stack, *, instance_id: str, username: str) -> RemoteUser:
    """Mirror a peer's user into ``remote_users`` the way a peer-directory
    snapshot would.
    """
    await _seed_remote_instance(stack, instance_id)
    user_id = f"uid-{username}-remote"
    remote = RemoteUser(
        user_id=user_id,
        instance_id=instance_id,
        remote_username=username,
        display_name=username.title(),
    )
    await stack.user_repo.upsert_remote(remote)
    return remote


async def test_create_dm_with_other_user_id_seats_remote_member(stack):
    await stack.provision("anna")
    bob = await _seed_remote_user(stack, instance_id="peer-b", username="bob")

    dm = await stack.dm_svc.create_dm(
        creator_username="anna",
        other_user_id=bob.user_id,
    )

    assert dm.type is ConversationType.DM
    locals_ = await stack.conv_repo.list_members(dm.id)
    remotes = await stack.conv_repo.list_remote_members(dm.id)
    assert {m.username for m in locals_} == {"anna"}
    assert {(m.instance_id, m.remote_username) for m in remotes} == {
        ("peer-b", "bob"),
    }


async def test_create_dm_with_other_user_id_is_idempotent(stack):
    await stack.provision("anna")
    bob = await _seed_remote_user(stack, instance_id="peer-b", username="bob")
    a = await stack.dm_svc.create_dm(
        creator_username="anna", other_user_id=bob.user_id,
    )
    b = await stack.dm_svc.create_dm(
        creator_username="anna", other_user_id=bob.user_id,
    )
    assert a.id == b.id


async def test_create_dm_rejects_zero_or_two_targets(stack):
    await stack.provision("anna")
    with pytest.raises(ValueError, match="exactly one"):
        await stack.dm_svc.create_dm(creator_username="anna")
    with pytest.raises(ValueError, match="exactly one"):
        await stack.dm_svc.create_dm(
            creator_username="anna",
            other_username="bob",
            other_user_id="uid-bob",
        )


async def test_create_dm_unknown_remote_user_raises(stack):
    await stack.provision("anna")
    with pytest.raises(KeyError, match="not found"):
        await stack.dm_svc.create_dm(
            creator_username="anna", other_user_id="uid-nope",
        )


async def test_send_to_remote_member_includes_user_id_in_envelope(stack):
    """``recipient_user_ids`` must carry the *peer-side* user_id so the
    receiver can seat the right local user.
    """
    await stack.provision("anna")
    bob = await _seed_remote_user(stack, instance_id="peer-b", username="bob")
    fed = _FakeFederationService()
    repo = _FakeFederationRepo({"peer-b": _confirmed_peer("peer-b")})
    stack.dm_svc.attach_federation(fed, repo, own_instance_id=stack.own_instance_id)

    dm = await stack.dm_svc.create_dm(
        creator_username="anna",
        other_user_id=bob.user_id,
    )
    await stack.dm_svc.send_message(
        dm.id, sender_username="anna", content="hi bob",
    )

    sent = [s for s in fed.sent if s["type"] == FederationEventType.DM_MESSAGE]
    assert len(sent) == 1
    payload = sent[0]["payload"]
    assert payload["conversation_id"] == dm.id
    assert payload["content"] == "hi bob"
    assert bob.user_id in payload["recipient_user_ids"]
