"""Tests for socialhome.domain.space."""

from __future__ import annotations

import pytest

from socialhome.domain.space import (
    HouseholdFeatures,
    SpaceConfigGapError,
    SpaceFeatureAccess,
    SpaceFeatures,
    SpacePermissionError,
)


def test_space_features_roundtrip():
    """SpaceFeatures survives a to_columns / from_row round-trip."""
    f = SpaceFeatures(calendar=True, tasks_access=SpaceFeatureAccess.MODERATED)
    f2 = SpaceFeatures.from_row(f.to_columns())
    assert f == f2


def test_space_features_gallery_roundtrip():
    """gallery flag survives to_columns / from_row + appears on the wire."""
    on = SpaceFeatures(gallery=True)
    off = SpaceFeatures(gallery=False)
    assert SpaceFeatures.from_row(on.to_columns()).gallery is True
    assert SpaceFeatures.from_row(off.to_columns()).gallery is False
    assert on.to_wire_dict()["gallery"] is True
    assert off.to_wire_dict()["gallery"] is False


def test_space_features_gallery_default_on_missing_column():
    """A row missing ``feature_gallery`` (pre-0008 spaces from a peer
    that hasn't migrated yet) defaults to gallery=True so the tab
    stays visible — matches the dataclass default."""
    f = SpaceFeatures.from_row({})  # no feature_gallery column at all
    assert f.gallery is True


def test_space_features_access_decision():
    """access_decision returns proceed/queue/deny based on access level and admin status."""
    f = SpaceFeatures(posts_access=SpaceFeatureAccess.MODERATED)
    assert f.access_decision("posts", is_admin=True) == "proceed"
    assert f.access_decision("posts", is_admin=False) == "queue"
    f2 = SpaceFeatures(posts_access=SpaceFeatureAccess.ADMIN_ONLY)
    assert f2.access_decision("posts", is_admin=False) == "deny"


def test_space_features_with_allowed_post_types():
    """with_allowed_post_types normalises and stores the set; empty set raises ValueError."""
    f = SpaceFeatures()
    f2 = f.with_allowed_post_types({"text", "image"})
    assert f2.allowed_post_types == ("image", "text")
    with pytest.raises(ValueError):
        f.with_allowed_post_types(set())


def test_household_features_roundtrip():
    """HouseholdFeatures survives a to_columns / from_row round-trip."""
    h = HouseholdFeatures(bazaar=False, household_name="Casa")
    h2 = HouseholdFeatures.from_row(h.to_columns())
    assert h == h2


def test_permission_error_banned():
    """SpacePermissionError with banned=True exposes the flag and a useful message."""
    e = SpacePermissionError("banned", banned=True)
    assert e.banned and "banned" in str(e)


def test_config_gap_error():
    """SpaceConfigGapError includes space_id, have, and need in its string form."""
    e = SpaceConfigGapError(space_id="s1", have=3, need=7)
    assert "s1" in str(e) and "3" in str(e)
