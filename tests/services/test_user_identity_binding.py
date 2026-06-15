"""user_identity_binding — derive the per-user identity-binding wire fields.

The helper is shared by the two outbound services that publish user info
to peers (``users_sync_outbound`` and ``profile_federation_outbound``). It
emits the ``user_identity_public_key`` / ``user_sig_suite`` /
``user_signature`` binding fields ONLY for peers that advertise v_25, and
only for users who actually have a minted identity key; everything else
gets the empty dict so the legacy payload shape is unchanged.
"""

from __future__ import annotations

import pytest

from socialhome.crypto import (
    USER_SIG_SUITE_ED25519,
    build_user_identity_assertion,
    derive_instance_id,
    derive_user_id,
    generate_identity_keypair,
    verify_user_identity_assertion,
)
from socialhome.domain.federation_capabilities import FederationCapability
from socialhome.domain.user import UserIdentityAssertion
from socialhome.services.user_identity_binding import user_identity_binding_fields


class _FakeFederation:
    def __init__(self, instance_seed: bytes, instance_pk: bytes, *, supports: bool):
        self._own_identity_seed = instance_seed
        self._own_identity_pk = instance_pk
        self._own_instance_id = derive_instance_id(instance_pk)
        self._supports = supports
        self.support_calls: list[tuple[str, int]] = []

    @property
    def own_identity_seed(self) -> bytes:
        return self._own_identity_seed

    @property
    def own_instance_id(self) -> str:
        return self._own_instance_id

    async def peer_supports(self, instance_id: str, *, min_version: int) -> bool:
        self.support_calls.append((instance_id, min_version))
        return self._supports


class _FakeUserRepo:
    def __init__(self, keypairs: dict[str, tuple[bytes, bytes]]):
        self._keypairs = keypairs

    async def get_user_identity_keypair(self, username: str):
        return self._keypairs.get(username)


@pytest.fixture
def instance_kp():
    return generate_identity_keypair()


@pytest.fixture
def user_kp():
    return generate_identity_keypair()


async def test_v25_peer_gets_verifiable_binding(instance_kp, user_kp):
    fed = _FakeFederation(
        instance_kp.private_key, instance_kp.public_key, supports=True
    )
    iid = derive_instance_id(instance_kp.public_key)
    uid = derive_user_id(instance_kp.public_key, "alice")
    repo = _FakeUserRepo({"alice": (user_kp.public_key, user_kp.private_key)})

    fields = await user_identity_binding_fields(
        federation_service=fed,
        user_repo=repo,
        peer_instance_id="peer-1",
        user_id=uid,
        username="alice",
        display_name="Alice",
    )

    # The entry now carries the FULL self-verifying assertion: the three
    # issued_at-independent binding fields PLUS the instance signature
    # (``user_assertion_signature``) and the assertion's ``issued_at``
    # (``user_assertion_issued_at``), so a relayed/cached copy can be
    # re-verified outside the original signed envelope.
    assert set(fields) == {
        "user_identity_public_key",
        "user_sig_suite",
        "user_signature",
        "user_assertion_signature",
        "user_assertion_issued_at",
    }
    assert fields["user_identity_public_key"] == user_kp.public_key.hex()
    assert fields["user_sig_suite"] == USER_SIG_SUITE_ED25519
    # The gate was checked against the v_25 threshold.
    assert fed.support_calls == [
        ("peer-1", FederationCapability.MIN_FOR_USER_IDENTITY_KEY)
    ]

    # The emitted fields reconstruct an assertion that verifies end-to-end
    # against the issuing instance's public key — proving the entry is a
    # standalone, self-verifying portable credential.
    reconstructed = UserIdentityAssertion(
        user_id=uid,
        instance_id=iid,
        username="alice",
        display_name="Alice",
        issued_at=fields["user_assertion_issued_at"],
        signature=fields["user_assertion_signature"],
        user_identity_public_key=fields["user_identity_public_key"],
        user_pq_public_key=None,
        user_sig_suite=fields["user_sig_suite"],
        user_signature=fields["user_signature"],
    )
    verify_user_identity_assertion(reconstructed, instance_kp.public_key)

    # The three binding fields are issued_at-independent, so they match a
    # reference assertion regardless of when it was minted.
    reference = build_user_identity_assertion(
        instance_seed=instance_kp.private_key,
        user_id=uid,
        instance_id=iid,
        username="alice",
        display_name="Alice",
        issued_at="2026-06-15T00:00:00+00:00",
        user_seed=user_kp.private_key,
        user_public_key=user_kp.public_key,
        user_sig_suite=USER_SIG_SUITE_ED25519,
    )
    assert fields["user_identity_public_key"] == reference.user_identity_public_key
    assert fields["user_sig_suite"] == reference.user_sig_suite
    assert fields["user_signature"] == reference.user_signature


async def test_sub_v25_peer_gets_no_binding(instance_kp, user_kp):
    fed = _FakeFederation(
        instance_kp.private_key, instance_kp.public_key, supports=False
    )
    uid = derive_user_id(instance_kp.public_key, "alice")
    repo = _FakeUserRepo({"alice": (user_kp.public_key, user_kp.private_key)})

    fields = await user_identity_binding_fields(
        federation_service=fed,
        user_repo=repo,
        peer_instance_id="peer-1",
        user_id=uid,
        username="alice",
        display_name="Alice",
    )
    assert fields == {}


async def test_v25_peer_no_minted_key_gets_no_binding(instance_kp):
    fed = _FakeFederation(
        instance_kp.private_key, instance_kp.public_key, supports=True
    )
    uid = derive_user_id(instance_kp.public_key, "alice")
    repo = _FakeUserRepo({})  # no keypair for alice

    fields = await user_identity_binding_fields(
        federation_service=fed,
        user_repo=repo,
        peer_instance_id="peer-1",
        user_id=uid,
        username="alice",
        display_name="Alice",
    )
    assert fields == {}


async def test_no_user_repo_gets_no_binding(instance_kp):
    fed = _FakeFederation(
        instance_kp.private_key, instance_kp.public_key, supports=True
    )
    uid = derive_user_id(instance_kp.public_key, "alice")

    fields = await user_identity_binding_fields(
        federation_service=fed,
        user_repo=None,
        peer_instance_id="peer-1",
        user_id=uid,
        username="alice",
        display_name="Alice",
    )
    assert fields == {}
