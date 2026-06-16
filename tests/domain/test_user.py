"""Tests for socialhome.domain.user."""

from __future__ import annotations


from socialhome.domain.user import (
    DisplayableUser,
    MemberRef,
    RESERVED_USERNAMES,
    RemoteUser,
    User,
    UserIdentityAssertion,
)


def test_member_ref_is_local():
    """is_local returns True only when the instance_id matches."""
    m = MemberRef(user_id="u1", instance_id="i1", username="alice")
    assert m.is_local("i1") is True
    assert m.is_local("i2") is False


def test_member_ref_str():
    """str(MemberRef) returns the user_id."""
    m = MemberRef(user_id="u1", instance_id="i1", username="alice")
    assert str(m) == "u1"


def test_displayable_user_from_local():
    """from_local_user builds a DisplayableUser with alias and is_local=True."""
    u = User(user_id="u1", username="alice", display_name="Alice")
    d = DisplayableUser.from_local_user(u, "i1", alias="Ali")
    assert d.display_name == "Ali" and d.is_local


def test_displayable_user_space_alias_wins():
    """Space alias takes precedence over personal alias in display_name."""
    u = User(user_id="u1", username="alice", display_name="Alice")
    d = DisplayableUser.from_local_user(u, "i1", alias="Ali", space_alias="Mom")
    assert d.display_name == "Mom" and d.has_space_alias


def test_displayable_user_from_remote_user():
    """from_remote_user produces a non-local DisplayableUser."""
    ru = RemoteUser(
        user_id="ru", instance_id="i1", remote_username="bob", display_name="Bob"
    )
    d = DisplayableUser.from_remote_user(ru)
    assert not d.is_local and d.username == "bob"


def test_user_is_active():
    """is_active returns False for inactive state or a deleted_at timestamp."""
    u = User(user_id="u", username="a", display_name="A")
    assert u.is_active()
    u2 = User(user_id="u", username="a", display_name="A", state="inactive")
    assert not u2.is_active()
    u3 = User(user_id="u", username="a", display_name="A", deleted_at="2026-01-01")
    assert not u3.is_active()


def test_user_reserved_usernames_nonempty():
    """RESERVED_USERNAMES is a non-empty collection."""
    assert len(RESERVED_USERNAMES) > 0
    assert "admin" in RESERVED_USERNAMES


def test_user_identity_assertion_user_binding_fields_default_none():
    """The Phase-1 user-binding fields are nullable and default to None so a
    legacy / first-revision payload that omits them still constructs."""
    a = UserIdentityAssertion(
        user_id="u1",
        instance_id="i1",
        username="alice",
        display_name="Alice",
        issued_at="2026-01-01T00:00:00+00:00",
        signature="sig",
    )
    assert a.user_identity_public_key is None
    assert a.user_pq_public_key is None
    assert a.user_sig_suite is None
    assert a.user_signature is None
    assert a.identity_anchor is None


def test_user_identity_assertion_carries_identity_anchor():
    """The optional ``identity_anchor`` field is settable and defaults None so
    legacy payloads still construct."""
    a = UserIdentityAssertion(
        user_id="u1",
        instance_id="i1",
        username="alice",
        display_name="Alice",
        issued_at="2026-01-01T00:00:00+00:00",
        signature="sig",
        identity_anchor="deadbeef",
    )
    assert a.identity_anchor == "deadbeef"


def test_user_identity_assertion_wire_roundtrip_full_field_set():
    """``to_wire_dict``/``from_wire_dict`` round-trip every field of a
    binding-bearing assertion (the full set used by the federation inbound
    ``_store_user_identity_binding`` path)."""
    a = UserIdentityAssertion(
        user_id="u1",
        instance_id="i1",
        username="alice",
        display_name="Alice",
        issued_at="2026-01-01T00:00:00+00:00",
        signature="instance-sig",
        picture_hash="abc123",
        public_key="ecdh-pk",
        public_key_version=3,
        user_identity_public_key="deadbeefuserpk",
        user_pq_public_key=None,
        user_sig_suite="ed25519",
        user_signature="user-self-sig",
        identity_anchor="anchoruuid",
    )
    assert UserIdentityAssertion.from_wire_dict(a.to_wire_dict()) == a


def test_user_identity_assertion_wire_roundtrip_legacy_omits_binding():
    """A legacy / first-revision assertion (no binding fields) round-trips with
    its defaults intact."""
    a = UserIdentityAssertion(
        user_id="u1",
        instance_id="i1",
        username="alice",
        display_name="Alice",
        issued_at="2026-01-01T00:00:00+00:00",
        signature="instance-sig",
    )
    assert UserIdentityAssertion.from_wire_dict(a.to_wire_dict()) == a
