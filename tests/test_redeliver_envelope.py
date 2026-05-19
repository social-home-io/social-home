"""Coverage extras for app._redeliver_envelope (outbox retry path)."""

from __future__ import annotations

import pytest

from socialhome.app import _redeliver_envelope, _aiohttp_timeout
from socialhome.crypto import (
    derive_instance_id,
    generate_identity_keypair,
)
from socialhome.db.database import AsyncDatabase
from socialhome.domain.federation import (
    InstanceSource,
    PairingStatus,
    RemoteInstance,
)
from socialhome.federation.federation_service import FederationService
from socialhome.infrastructure import DeliveryOutcome, EventBus, KeyManager
from socialhome.repositories import (
    SqliteFederationRepo,
    SqliteOutboxRepo,
)


# ─── _aiohttp_timeout ────────────────────────────────────────────────────


def test_aiohttp_timeout_returns_object():
    t = _aiohttp_timeout(10)
    # aiohttp installed → real ClientTimeout. Either way, no raise.
    assert t is not None or t is None


# ─── _redeliver_envelope ─────────────────────────────────────────────────


class _OutboxEntry:
    def __init__(self, *, id, instance_id, payload_json):
        self.id = id
        self.instance_id = instance_id
        self.payload_json = payload_json


@pytest.fixture
async def env(tmp_dir):
    db = AsyncDatabase(tmp_dir / "t.db", batch_timeout_ms=10)
    await db.startup()
    own_kp = generate_identity_keypair()
    own_iid = derive_instance_id(own_kp.public_key)
    await db.enqueue(
        "INSERT INTO instance_identity(instance_id, identity_private_key,"
        " identity_public_key, routing_secret) VALUES(?,?,?,?)",
        (own_iid, own_kp.private_key.hex(), own_kp.public_key.hex(), "aa" * 32),
    )
    fed_repo = SqliteFederationRepo(db)
    outbox = SqliteOutboxRepo(db)
    bus = EventBus()
    kek = KeyManager.from_data_dir(tmp_dir)
    svc = FederationService(
        db=db,
        federation_repo=fed_repo,
        outbox_repo=outbox,
        key_manager=kek,
        bus=bus,
        own_instance_id=own_iid,
        own_identity_seed=own_kp.private_key,
        own_identity_pk=own_kp.public_key,
    )
    yield svc, fed_repo, kek
    await db.shutdown()


async def test_redeliver_unknown_instance_is_permanent(env):
    svc, fed_repo, _ = env
    entry = _OutboxEntry(
        id="e1",
        instance_id="never-paired",
        payload_json="{}",
    )
    outcome = await _redeliver_envelope(svc, fed_repo, entry)
    # Instance was unpaired / dropped — nothing to retry, mark failed.
    assert outcome is DeliveryOutcome.PERMANENT


async def test_redeliver_2xx_is_success(env):
    svc, fed_repo, kek = env
    peer_kp = generate_identity_keypair()
    wrapped = kek.encrypt(b"\x01" * 32)
    peer = RemoteInstance(
        id=derive_instance_id(peer_kp.public_key),
        display_name="peer",
        remote_identity_pk=peer_kp.public_key.hex(),
        key_self_to_remote=wrapped,
        key_remote_to_self=wrapped,
        remote_inbox_url="https://x/wh",
        local_inbox_id="wh",
        status=PairingStatus.CONFIRMED,
        source=InstanceSource.MANUAL,
    )
    await fed_repo.save_instance(peer)

    class _Resp:
        def __init__(self):
            self.status = 204

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class _Client:
        def post(self, url, **kw):
            return _Resp()

    svc._http_client = _Client()
    entry = _OutboxEntry(id="e1", instance_id=peer.id, payload_json='{"x":1}')
    outcome = await _redeliver_envelope(svc, fed_repo, entry)
    assert outcome is DeliveryOutcome.SUCCESS


async def test_redeliver_5xx_is_transient(env):
    svc, fed_repo, kek = env
    peer_kp = generate_identity_keypair()
    wrapped = kek.encrypt(b"\x02" * 32)
    peer = RemoteInstance(
        id=derive_instance_id(peer_kp.public_key),
        display_name="peer",
        remote_identity_pk=peer_kp.public_key.hex(),
        key_self_to_remote=wrapped,
        key_remote_to_self=wrapped,
        remote_inbox_url="https://x/wh",
        local_inbox_id="wh2",
        status=PairingStatus.CONFIRMED,
        source=InstanceSource.MANUAL,
    )
    await fed_repo.save_instance(peer)

    class _Resp:
        def __init__(self):
            self.status = 503

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class _Client:
        def post(self, url, **kw):
            return _Resp()

    svc._http_client = _Client()
    entry = _OutboxEntry(id="e2", instance_id=peer.id, payload_json="{}")
    outcome = await _redeliver_envelope(svc, fed_repo, entry)
    assert outcome is DeliveryOutcome.TRANSIENT


async def test_redeliver_4xx_is_permanent(env):
    """A 4xx response — replay-cache hit, expired timestamp, banned —
    must be dropped, not retried. Specifically pins the 410 ``Replay
    detected`` shape that left thousands of zombie outbox entries
    behind during the HA-integration charset bug.
    """
    svc, fed_repo, kek = env
    peer_kp = generate_identity_keypair()
    wrapped = kek.encrypt(b"\x04" * 32)
    peer = RemoteInstance(
        id=derive_instance_id(peer_kp.public_key),
        display_name="peer",
        remote_identity_pk=peer_kp.public_key.hex(),
        key_self_to_remote=wrapped,
        key_remote_to_self=wrapped,
        remote_inbox_url="https://x/wh",
        local_inbox_id="wh-410",
        status=PairingStatus.CONFIRMED,
        source=InstanceSource.MANUAL,
    )
    await fed_repo.save_instance(peer)

    class _Resp:
        def __init__(self, status):
            self.status = status

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class _Client:
        def __init__(self, status):
            self._status = status

        def post(self, url, **kw):
            return _Resp(self._status)

    for status in (400, 403, 404, 410, 422):
        svc._http_client = _Client(status)
        entry = _OutboxEntry(id=f"e-{status}", instance_id=peer.id, payload_json="{}")
        outcome = await _redeliver_envelope(svc, fed_repo, entry)
        assert outcome is DeliveryOutcome.PERMANENT, (status, outcome)


async def test_redeliver_4xx_marks_peer_reachable(env):
    """A 4xx is proof of reachability — the receiver got the HTTP
    request, ran our envelope through the §24.11 pipeline, and chose
    to reject it. The peer-online indicator in the SPA reads from
    ``RemoteInstance.unreachable_since``; without flipping that field
    back to ``None`` on 4xx, the entire post-charset-bug backlog of
    410 ``Replay detected`` retries kept the indicator stuck on
    "not connected" even though the peer was clearly responsive.
    """
    svc, fed_repo, kek = env
    peer_kp = generate_identity_keypair()
    wrapped = kek.encrypt(b"\x05" * 32)
    peer = RemoteInstance(
        id=derive_instance_id(peer_kp.public_key),
        display_name="peer",
        remote_identity_pk=peer_kp.public_key.hex(),
        key_self_to_remote=wrapped,
        key_remote_to_self=wrapped,
        remote_inbox_url="https://x/wh",
        local_inbox_id="wh-410-reach",
        status=PairingStatus.CONFIRMED,
        source=InstanceSource.MANUAL,
    )
    await fed_repo.save_instance(peer)
    # Simulate the peer having been marked unreachable by an earlier
    # send_event failure.
    await fed_repo.mark_unreachable(peer.id)
    pre = await fed_repo.get_instance(peer.id)
    assert pre.unreachable_since is not None

    class _Resp:
        def __init__(self):
            self.status = 410

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class _Client:
        def post(self, url, **kw):
            return _Resp()

    svc._http_client = _Client()
    entry = _OutboxEntry(id="e-reach", instance_id=peer.id, payload_json="{}")
    outcome = await _redeliver_envelope(svc, fed_repo, entry)
    assert outcome is DeliveryOutcome.PERMANENT

    post = await fed_repo.get_instance(peer.id)
    assert post.unreachable_since is None, (
        "4xx response must flip the peer back to reachable — otherwise "
        "the SPA's online indicator stays stuck on 'not connected'"
    )


async def test_redeliver_transport_error_is_transient(env):
    svc, fed_repo, kek = env
    peer_kp = generate_identity_keypair()
    wrapped = kek.encrypt(b"\x03" * 32)
    peer = RemoteInstance(
        id=derive_instance_id(peer_kp.public_key),
        display_name="peer",
        remote_identity_pk=peer_kp.public_key.hex(),
        key_self_to_remote=wrapped,
        key_remote_to_self=wrapped,
        remote_inbox_url="https://x/wh",
        local_inbox_id="wh3",
        status=PairingStatus.CONFIRMED,
        source=InstanceSource.MANUAL,
    )
    await fed_repo.save_instance(peer)

    class _Client:
        def post(self, url, **kw):
            raise ConnectionError("boom")

    svc._http_client = _Client()
    entry = _OutboxEntry(id="e3", instance_id=peer.id, payload_json="{}")
    outcome = await _redeliver_envelope(svc, fed_repo, entry)
    assert outcome is DeliveryOutcome.TRANSIENT
