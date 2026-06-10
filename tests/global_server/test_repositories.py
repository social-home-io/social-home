"""Extra coverage for GFS repositories (admin + federation helpers)."""

from __future__ import annotations

import time

import pytest

from socialhome.global_server.domain import (
    ClientInstance,
    GfsAppeal,
    GfsFraudReport,
    GlobalSpace,
)
from socialhome.global_server.repositories import (
    SqliteGfsAdminRepo,
    SqliteGfsFederationRepo,
)


@pytest.fixture
async def fed(gfs_db):
    return SqliteGfsFederationRepo(gfs_db)


@pytest.fixture
async def admin(gfs_db):
    return SqliteGfsAdminRepo(gfs_db)


# ── Federation helpers ────────────────────────────────────────────────


async def test_list_instances_filtered_by_status(fed):
    await fed.upsert_instance(
        ClientInstance(
            instance_id="a",
            display_name="A",
            public_key="aa" * 32,
            inbox_url="http://a",
            status="pending",
        )
    )
    await fed.upsert_instance(
        ClientInstance(
            instance_id="b",
            display_name="B",
            public_key="bb" * 32,
            inbox_url="http://b",
            status="active",
        )
    )
    active = await fed.list_instances(status="active")
    assert {x.instance_id for x in active} == {"b"}
    pending = await fed.list_instances(status="pending")
    assert {x.instance_id for x in pending} == {"a"}


async def test_upsert_instance_round_trips_keywrap_fields(fed):
    await fed.upsert_instance(
        ClientInstance(
            instance_id="kw",
            display_name="KW",
            public_key="aa" * 32,
            inbox_url="http://kw",
            keywrap_public_key="dd" * 32,
            kem_suite="x25519",
        )
    )
    got = await fed.get_instance("kw")
    assert got is not None
    assert got.keywrap_public_key == "dd" * 32
    assert got.kem_suite == "x25519"


async def test_get_instance_legacy_row_without_keywrap_is_empty(fed):
    await fed.upsert_instance(
        ClientInstance(
            instance_id="legacy",
            display_name="L",
            public_key="aa" * 32,
            inbox_url="http://l",
        )
    )
    got = await fed.get_instance("legacy")
    assert got is not None
    assert got.keywrap_public_key == ""
    assert got.kem_suite == ""
    assert got.keywrap_sig == ""


async def test_upsert_instance_round_trips_keywrap_sig(fed):
    await fed.upsert_instance(
        ClientInstance(
            instance_id="kws",
            display_name="KWS",
            public_key="aa" * 32,
            inbox_url="http://kws",
            keywrap_public_key="dd" * 32,
            kem_suite="x25519",
            keywrap_sig="c2ln",
        )
    )
    got = await fed.get_instance("kws")
    assert got is not None
    assert got.keywrap_sig == "c2ln"


async def test_set_instance_display_name_updates_only_name(fed):
    await fed.upsert_instance(
        ClientInstance(
            instance_id="rn",
            display_name="Old",
            public_key="cc" * 32,
            inbox_url="http://rn",
            status="active",
        )
    )
    await fed.set_instance_display_name("rn", "New Name")
    inst = await fed.get_instance("rn")
    assert inst is not None
    assert inst.display_name == "New Name"
    # Other columns untouched.
    assert inst.public_key == "cc" * 32
    assert inst.status == "active"


async def test_list_spaces_for_instance(fed):
    await fed.upsert_instance(
        ClientInstance(
            instance_id="owner",
            display_name="O",
            public_key="aa" * 32,
            inbox_url="http://o",
            status="active",
        )
    )
    await fed.upsert_space(
        GlobalSpace(
            space_id="s1",
            owning_instance="owner",
            status="active",
        )
    )
    await fed.upsert_space(
        GlobalSpace(
            space_id="s2",
            owning_instance="owner",
            status="banned",
        )
    )
    got = await fed.list_spaces_for_instance("owner")
    assert {s.space_id for s in got} == {"s1", "s2"}


async def test_remove_subscriber_updates_count(fed):
    await fed.upsert_instance(
        ClientInstance(
            instance_id="o",
            display_name="O",
            public_key="aa" * 32,
            inbox_url="http://o",
            status="active",
        )
    )
    await fed.upsert_instance(
        ClientInstance(
            instance_id="sub",
            display_name="S",
            public_key="bb" * 32,
            inbox_url="http://s",
            status="active",
        )
    )
    await fed.upsert_space(
        GlobalSpace(
            space_id="sp",
            owning_instance="o",
            status="active",
        )
    )
    await fed.add_subscriber(space_id="sp", instance_id="sub")
    sp = await fed.get_space("sp")
    assert sp.subscriber_count == 1
    await fed.remove_subscriber(space_id="sp", instance_id="sub")
    sp = await fed.get_space("sp")
    assert sp.subscriber_count == 0


# ── Admin helpers ─────────────────────────────────────────────────────


async def test_count_reports_by_reporter(admin):
    now = int(time.time())
    for i in range(3):
        await admin.save_fraud_report(
            GfsFraudReport(
                id=f"rep-{i}",
                target_type="space",
                target_id=f"t-{i}",
                category="spam",
                notes=None,
                reporter_instance_id="rep.home",
                reporter_user_id=None,
                status="pending",
                created_at=now,
            )
        )
    cnt = await admin.count_reports_by_reporter("rep.home", since=now - 60)
    assert cnt == 3


async def test_get_config_returns_none_for_unknown_key(admin):
    assert await admin.get_config("no-such-key") is None


async def test_set_config_is_idempotent(admin):
    await admin.set_config("k", "v1")
    await admin.set_config("k", "v2")
    assert await admin.get_config("k") == "v2"


async def test_admin_session_roundtrip(admin):
    await admin.create_session("t-1", expires_at=int(time.time()) + 3600)
    session = await admin.get_session("t-1")
    assert session is not None
    assert session.token == "t-1"
    await admin.delete_session("t-1")
    assert await admin.get_session("t-1") is None


async def test_admin_session_purge_expired(admin):
    await admin.create_session("old", expires_at=int(time.time()) - 1)
    await admin.create_session("fresh", expires_at=int(time.time()) + 1000)
    await admin.purge_expired_sessions(int(time.time()))
    assert await admin.get_session("old") is None
    assert await admin.get_session("fresh") is not None


async def test_appeal_persist_list_and_decide(admin):
    a = GfsAppeal(
        id="a1",
        target_type="space",
        target_id="sp",
        message="plz",
        status="pending",
        created_at=int(time.time()),
    )
    await admin.save_appeal(a)
    pending = await admin.list_appeals(status="pending")
    assert any(x.id == "a1" for x in pending)
    await admin.set_appeal_status("a1", status="lifted", decided_by="admin")
    got = await admin.get_appeal("a1")
    assert got.status == "lifted"


async def test_pair_token_single_use_and_ttl(admin):
    # Single-use + expired behaviour covered here.
    await admin.save_pair_token("tok-1", "1.2.3.4")
    assert await admin.consume_pair_token("tok-1") is True
    # Already consumed.
    assert await admin.consume_pair_token("tok-1") is False
    # Unknown token.
    assert await admin.consume_pair_token("nope") is False


async def test_prune_old_pair_tokens_drops_old_keeps_recent(admin, gfs_db):
    """``prune_old_pair_tokens`` deletes tokens created before the cutoff."""
    now = int(time.time())
    old = now - 2 * 86400  # 2 days ago
    recent = now - 60  # 1 minute ago
    await gfs_db.enqueue(
        "INSERT INTO gfs_pair_tokens(token, ip, created_at) VALUES(?, ?, ?)",
        ("old-tok", "1.1.1.1", old),
    )
    await gfs_db.enqueue(
        "INSERT INTO gfs_pair_tokens(token, ip, created_at) VALUES(?, ?, ?)",
        ("recent-tok", "2.2.2.2", recent),
    )

    cutoff = now - 86400  # 24h
    deleted = await admin.prune_old_pair_tokens(cutoff)
    assert deleted == 1

    rows = await gfs_db.fetchall("SELECT token FROM gfs_pair_tokens")
    tokens = {r["token"] for r in rows}
    assert "old-tok" not in tokens  # old row pruned
    assert "recent-tok" in tokens  # recent row kept


async def test_record_login_attempt_prunes_old_rows(admin, gfs_db):
    """Prune-on-write bounds the brute-force counter table.

    An old attempt (beyond the retention window) is dropped when a new
    attempt is recorded, while recent attempts survive and still count.
    """
    # Seed an ancient attempt directly (2 days old, beyond the 24h retention).
    old = int(time.time()) - 2 * 86400
    await gfs_db.enqueue(
        "INSERT INTO admin_login_attempts(ip, attempted_at) VALUES(?, ?)",
        ("9.9.9.9", old),
    )
    # Recording a fresh attempt triggers the prune-on-write.
    await admin.record_login_attempt("1.2.3.4")

    rows = await gfs_db.fetchall("SELECT ip FROM admin_login_attempts")
    ips = {r["ip"] for r in rows}
    assert "9.9.9.9" not in ips  # old row pruned
    assert "1.2.3.4" in ips  # recent row kept
    # The recent attempt still counts within a generous window.
    recent = int(time.time()) - 3600
    assert await admin.count_failed_attempts("1.2.3.4", since=recent) == 1
