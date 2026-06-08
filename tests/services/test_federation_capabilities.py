"""Tests for socialhome.domain.federation_capabilities feature labelling."""

from __future__ import annotations

from socialhome.domain.federation_capabilities import (
    CAPABILITY_FEATURES,
    OURS,
    SPACE_SCOPED_MIN_VERSIONS,
    FederationCapability,
    features_missing_below,
    space_features_missing_below,
)


def test_ours_is_v20_with_space_sync_rejected_capability():
    """v_20 introduces SPACE_SYNC_REJECTED gating (v_19 added INSTANCE_RESYNC)."""
    assert OURS == 20
    assert FederationCapability.MIN_FOR_INSTANCE_RESYNC == 19
    assert FederationCapability.MIN_FOR_SPACE_SYNC_REJECTED == 20
    assert (
        FederationCapability.MIN_FOR_SPACE_SYNC_REJECTED,
        "Space sync reject reconcile",
    ) in CAPABILITY_FEATURES


def test_features_built_from_min_for_constants():
    """Every CAPABILITY_FEATURES version maps to a MIN_FOR_* constant.

    The labels are a single source of truth derived FROM the constants
    — never a second hardcoded copy of the version numbers.
    """
    declared = {
        v
        for k, v in vars(FederationCapability).items()
        if k.startswith("MIN_FOR_") and isinstance(v, int)
    }
    feature_versions = {ver for ver, _ in CAPABILITY_FEATURES}
    assert feature_versions == declared
    assert len(CAPABILITY_FEATURES) == len(declared)


def test_features_missing_below_ours_is_empty():
    """A peer at OURS lacks nothing."""
    assert features_missing_below(OURS) == []


def test_features_missing_below_v1_lists_everything():
    """A v1 peer lacks every labelled feature."""
    missing = features_missing_below(1)
    assert missing == [label for _, label in sorted(CAPABILITY_FEATURES)]
    assert len(missing) == len(CAPABILITY_FEATURES)


def test_features_missing_below_mid_version():
    """A mid-version peer lacks only features above its version."""
    missing = features_missing_below(13)
    # Sync HTTPS fallback (v13) is supported -> not missing.
    assert "Sync HTTPS fallback" not in missing
    # Media DataChannel (v14) is above 13 -> missing.
    assert "Media DataChannel" in missing
    expected = [label for ver, label in sorted(CAPABILITY_FEATURES) if ver > 13]
    assert missing == expected


def test_space_features_missing_below_ours_is_empty():
    """A member household at OURS lacks no space feature."""
    assert space_features_missing_below(OURS) == []


def test_space_features_missing_below_v1_lists_only_space_scoped():
    """A v1 member household lacks exactly the space-scoped labels."""
    missing = space_features_missing_below(1)
    expected = [
        label
        for ver, label in sorted(CAPABILITY_FEATURES)
        if ver in SPACE_SCOPED_MIN_VERSIONS
    ]
    assert missing == expected
    # Non-space features are excluded even though a v1 peer lacks them too.
    assert "Calendar timezones" not in missing
    assert "DM media" not in missing
    assert "Home-location sharing" not in missing
    assert "App federation channel" not in missing
    assert "App user routing" not in missing


def test_space_features_missing_below_v13():
    """A v13 member household lacks the three space features above v13."""
    assert space_features_missing_below(13) == [
        "Media DataChannel",
        "Remote admin actions",
        "Multi-admin approvals",
    ]


def test_space_features_missing_below_v16_is_empty():
    """Nothing space-scoped lives above v16 — a v16 member lacks none."""
    assert space_features_missing_below(16) == []


def test_space_scoped_min_versions_are_capability_constants():
    """Every space-scoped threshold is a MIN_FOR_* int — no magic numbers."""
    declared = {
        v
        for k, v in vars(FederationCapability).items()
        if k.startswith("MIN_FOR_") and isinstance(v, int)
    }
    assert SPACE_SCOPED_MIN_VERSIONS <= declared
