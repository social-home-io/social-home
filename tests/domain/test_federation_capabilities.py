"""Tests for the federation protocol-version constants."""

from __future__ import annotations

from socialhome.domain import federation_capabilities as fc


def test_ours_is_current_version():
    assert fc.OURS == 18


def test_remote_admin_action_capability_threshold():
    assert fc.FederationCapability.MIN_FOR_REMOTE_ADMIN_ACTION == 15
    assert fc.FederationCapability.MIN_FOR_REMOTE_ADMIN_ACTION <= fc.OURS


def test_admin_proposals_capability_threshold():
    assert fc.FederationCapability.MIN_FOR_ADMIN_PROPOSALS == 16
    assert fc.FederationCapability.MIN_FOR_ADMIN_PROPOSALS <= fc.OURS


def test_app_channel_capability_threshold():
    assert fc.FederationCapability.MIN_FOR_APP_CHANNEL == 17
    assert fc.FederationCapability.MIN_FOR_APP_CHANNEL <= fc.OURS


def test_ours_is_at_least_18_and_app_user_routing_constant():
    from socialhome.domain.federation_capabilities import OURS, FederationCapability

    assert OURS >= 18
    assert FederationCapability.MIN_FOR_APP_USER_ROUTING == 18


def test_media_channel_capability_threshold():
    assert fc.FederationCapability.MIN_FOR_MEDIA_CHANNEL == 14
    # Gating constants must never exceed what this build advertises, or
    # a sender would gate a feature on a version no peer can reach.
    assert fc.FederationCapability.MIN_FOR_MEDIA_CHANNEL <= fc.OURS


def test_named_thresholds_are_monotonic_and_bounded():
    """Every named capability threshold is a positive int ≤ OURS."""
    named = {
        k: v
        for k, v in vars(fc.FederationCapability).items()
        if k.startswith("MIN_FOR_")
    }
    assert named  # sanity: there are named thresholds
    for name, version in named.items():
        assert isinstance(version, int), name
        assert 1 <= version <= fc.OURS, f"{name}={version} out of range"
