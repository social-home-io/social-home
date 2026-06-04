"""Tests for socialhome.domain.federation_capabilities feature labelling."""

from __future__ import annotations

from socialhome.domain.federation_capabilities import (
    CAPABILITY_FEATURES,
    OURS,
    FederationCapability,
    features_missing_below,
)


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
