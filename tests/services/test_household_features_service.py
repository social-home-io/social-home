"""Tests for HouseholdFeaturesService (§22)."""

from __future__ import annotations

import pytest

from socialhome.crypto import (
    derive_instance_id,
    generate_identity_keypair,
)
from socialhome.db.database import AsyncDatabase
from socialhome.domain.space import SpacePermissionError
from socialhome.repositories.household_features_repo import (
    SqliteHouseholdFeaturesRepo,
)
from socialhome.services.household_features_service import (
    HouseholdFeatures,
    HouseholdFeaturesService,
)


@pytest.fixture
async def env(tmp_dir):
    kp = generate_identity_keypair()
    iid = derive_instance_id(kp.public_key)
    db = AsyncDatabase(tmp_dir / "t.db", batch_timeout_ms=10)
    await db.startup()
    await db.enqueue(
        "INSERT INTO instance_identity(instance_id, identity_private_key,"
        " identity_public_key, routing_secret) VALUES(?,?,?,?)",
        (iid, kp.private_key.hex(), kp.public_key.hex(), "aa" * 32),
    )
    yield HouseholdFeaturesService(SqliteHouseholdFeaturesRepo(db)), db
    await db.shutdown()


# ─── Reads ───────────────────────────────────────────────────────────────


async def test_get_returns_defaults_when_unset(env):
    svc, _ = env
    feats = await svc.get()
    assert isinstance(feats, HouseholdFeatures)
    assert feats.household_name == "Home"
    assert feats.feat_feed is True
    assert feats.allow_text is True


# ─── Writes ──────────────────────────────────────────────────────────────


async def test_update_admin_changes_household_name(env):
    svc, _ = env
    await svc.update(actor_is_admin=True, household_name="The Rivendells")
    feats = await svc.get()
    assert feats.household_name == "The Rivendells"


async def test_update_admin_changes_toggles(env):
    svc, _ = env
    await svc.update(
        actor_is_admin=True,
        toggles={"feat_pages": False, "allow_video": False},
    )
    feats = await svc.get()
    assert feats.feat_pages is False
    assert feats.allow_video is False
    # Untouched defaults survive.
    assert feats.feat_feed is True


async def test_update_non_admin_403(env):
    svc, _ = env
    with pytest.raises(SpacePermissionError):
        await svc.update(actor_is_admin=False, household_name="Hostile")


async def test_update_empty_name_422(env):
    svc, _ = env
    with pytest.raises(ValueError):
        await svc.update(actor_is_admin=True, household_name="")


async def test_update_too_long_name_422(env):
    svc, _ = env
    with pytest.raises(ValueError):
        await svc.update(actor_is_admin=True, household_name="x" * 200)


async def test_update_non_bool_toggle_422(env):
    svc, _ = env
    with pytest.raises(ValueError):
        await svc.update(actor_is_admin=True, toggles={"feat_feed": "yes"})


async def test_update_unknown_toggle_silently_ignored(env):
    svc, _ = env
    await svc.update(
        actor_is_admin=True,
        toggles={"unknown_key": True, "feat_pages": False},
    )
    feats = await svc.get()
    assert feats.feat_pages is False


async def test_update_no_args_returns_unchanged(env):
    svc, _ = env
    feats = await svc.update(actor_is_admin=True)
    assert feats.household_name == "Home"


# ─── Enforcement (§18) ───────────────────────────────────────────────────


async def test_require_enabled_passes_when_section_on(env):
    svc, _ = env
    # Default: all sections on.
    await svc.require_enabled("tasks")


async def test_require_enabled_raises_when_section_off(env):
    from socialhome.domain.household_features import FeatureDisabledError

    svc, _ = env
    await svc.update(actor_is_admin=True, toggles={"feat_tasks": False})
    with pytest.raises(FeatureDisabledError) as exc:
        await svc.require_enabled("tasks")
    assert exc.value.section == "tasks"


async def test_require_enabled_raises_on_unknown_section(env):
    from socialhome.domain.household_features import FeatureDisabledError

    svc, _ = env
    # Unknown section name → refuse. New features must flip the toggle
    # in the schema before being visible server-side.
    with pytest.raises(FeatureDisabledError):
        await svc.require_enabled("teleport")


async def test_require_post_type_blocks_disallowed_type(env):
    from socialhome.domain.household_features import FeatureDisabledError

    svc, _ = env
    await svc.update(actor_is_admin=True, toggles={"allow_video": False})
    with pytest.raises(FeatureDisabledError) as exc:
        await svc.require_post_type("video")
    assert "post_type:video" in exc.value.section


async def test_require_post_type_allows_default(env):
    svc, _ = env
    # All allow_* default to True.
    await svc.require_post_type("image")


# ─── HouseholdConfigChanged event (§23.13) ───────────────────────────────


async def test_update_publishes_household_config_changed(env, tmp_dir):
    from socialhome.domain.events import HouseholdConfigChanged
    from socialhome.infrastructure.event_bus import EventBus
    from socialhome.repositories.household_features_repo import (
        SqliteHouseholdFeaturesRepo,
    )

    _, db = env
    bus = EventBus()
    received = []

    async def _capture(evt):
        received.append(evt)

    bus.subscribe(HouseholdConfigChanged, _capture)
    svc_bus = HouseholdFeaturesService(
        SqliteHouseholdFeaturesRepo(db),
        bus=bus,
    )
    await svc_bus.update(actor_is_admin=True, toggles={"feat_pages": False})
    assert received
    assert received[0].changed == {"feat_pages": False}


async def test_update_no_change_no_event(env, tmp_dir):
    from socialhome.domain.events import HouseholdConfigChanged
    from socialhome.infrastructure.event_bus import EventBus
    from socialhome.repositories.household_features_repo import (
        SqliteHouseholdFeaturesRepo,
    )

    _, db = env
    bus = EventBus()
    received = []
    bus.subscribe(
        HouseholdConfigChanged,
        lambda e: received.append(e),  # type: ignore[arg-type]
    )
    svc_bus = HouseholdFeaturesService(
        SqliteHouseholdFeaturesRepo(db),
        bus=bus,
    )
    # Setting the same value as the current default → no change → no event.
    await svc_bus.update(actor_is_admin=True, toggles={"feat_feed": True})
    assert received == []


# ─── HA tz mirror — set_tz_from_ha ───────────────────────────────────────


async def test_set_tz_from_ha_writes_when_different(env, tmp_dir):
    """The HA tz poll writes the new value AND publishes a
    HouseholdConfigChanged event so connected clients refresh."""
    from socialhome.domain.events import HouseholdConfigChanged
    from socialhome.infrastructure.event_bus import EventBus
    from socialhome.repositories.household_features_repo import (
        SqliteHouseholdFeaturesRepo,
    )

    _, db = env
    bus = EventBus()
    received: list[HouseholdConfigChanged] = []
    bus.subscribe(
        HouseholdConfigChanged,
        lambda e: received.append(e),  # type: ignore[arg-type]
    )
    svc = HouseholdFeaturesService(
        SqliteHouseholdFeaturesRepo(db),
        bus=bus,
    )

    await svc.set_tz_from_ha("Europe/Berlin")

    feats = await svc.get()
    assert feats.tz == "Europe/Berlin"
    assert received and received[0].changed == {"tz": "Europe/Berlin"}


async def test_set_tz_from_ha_skips_when_unchanged(env, tmp_dir):
    """Idempotent — the HA adapter polls on every startup and we don't
    want the broadcast to fire every time."""
    from socialhome.domain.events import HouseholdConfigChanged
    from socialhome.infrastructure.event_bus import EventBus
    from socialhome.repositories.household_features_repo import (
        SqliteHouseholdFeaturesRepo,
    )

    _, db = env
    bus = EventBus()
    received: list[HouseholdConfigChanged] = []
    bus.subscribe(
        HouseholdConfigChanged,
        lambda e: received.append(e),  # type: ignore[arg-type]
    )
    svc = HouseholdFeaturesService(
        SqliteHouseholdFeaturesRepo(db),
        bus=bus,
    )

    await svc.set_tz_from_ha("Europe/Berlin")
    received.clear()
    # Second call with the same value — no DB write, no event.
    await svc.set_tz_from_ha("Europe/Berlin")
    assert received == []


async def test_set_tz_from_ha_ignores_unknown_zone(env):
    """An invalid IANA name (HA sometimes ships a malformed string) is
    silently dropped so the next poll can retry once HA's config is fixed."""
    svc, _ = env
    await svc.set_tz_from_ha("Not/AReal_Zone")
    # No change to the stored value.
    feats = await svc.get()
    assert feats.tz == "UTC"  # default


async def test_set_tz_from_ha_works_without_bus(env):
    """If no bus is wired (early bootstrap), the tz still persists —
    the event publish is best-effort."""
    _, db = env
    from socialhome.repositories.household_features_repo import (
        SqliteHouseholdFeaturesRepo,
    )

    svc = HouseholdFeaturesService(SqliteHouseholdFeaturesRepo(db))
    await svc.set_tz_from_ha("Asia/Tokyo")
    feats = await svc.get()
    assert feats.tz == "Asia/Tokyo"


# ─── Admin update — tz branch ────────────────────────────────────────────


async def test_update_sets_tz_when_valid(env):
    svc, _ = env
    after = await svc.update(actor_is_admin=True, tz="Europe/Berlin")
    assert after.tz == "Europe/Berlin"


async def test_update_rejects_empty_tz(env):
    svc, _ = env
    with pytest.raises(ValueError, match="tz must be"):
        await svc.update(actor_is_admin=True, tz="   ")


async def test_update_rejects_unknown_tz(env):
    svc, _ = env
    with pytest.raises(ValueError, match="unknown IANA timezone"):
        await svc.update(actor_is_admin=True, tz="Not/AReal_Zone")


async def test_update_tz_unchanged_no_op(env):
    """Re-setting tz to its current value doesn't trigger a DB write
    (covered by the existing ``set_household_name`` no-op pattern) and
    doesn't publish an event."""
    from socialhome.domain.events import HouseholdConfigChanged
    from socialhome.infrastructure.event_bus import EventBus
    from socialhome.repositories.household_features_repo import (
        SqliteHouseholdFeaturesRepo,
    )

    _, db = env
    bus = EventBus()
    received: list[HouseholdConfigChanged] = []
    bus.subscribe(
        HouseholdConfigChanged,
        lambda e: received.append(e),  # type: ignore[arg-type]
    )
    svc = HouseholdFeaturesService(
        SqliteHouseholdFeaturesRepo(db),
        bus=bus,
    )
    await svc.update(actor_is_admin=True, tz="Europe/Berlin")
    received.clear()
    await svc.update(actor_is_admin=True, tz="Europe/Berlin")
    assert received == []
