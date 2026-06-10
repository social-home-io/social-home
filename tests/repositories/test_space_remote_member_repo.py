"""Tests for socialhome.repositories.space_remote_member_repo."""

from __future__ import annotations

import pytest

from socialhome.db.database import AsyncDatabase
from socialhome.domain.space import SpaceRole
from socialhome.repositories.space_remote_member_repo import (
    SqliteSpaceRemoteMemberRepo,
)
from socialhome.repositories.space_repo import SqliteSpaceRepo


@pytest.fixture
async def repo(tmp_dir):
    db = AsyncDatabase(tmp_dir / "srm.db", batch_timeout_ms=10)
    await db.startup()
    # ``space_remote_members.space_id`` FKs ``spaces.id`` — seat a parent
    # row so the inserts below satisfy the constraint.
    spaces = SqliteSpaceRepo(db)
    from socialhome.domain.space import (
        JoinMode,
        Space,
        SpaceFeatures,
        SpaceType,
    )

    await spaces.save(
        Space(
            id="sp1",
            name="S",
            owner_instance_id="host",
            owner_username="anna",
            identity_public_key="00" * 32,
            config_sequence=0,
            features=SpaceFeatures(),
            space_type=SpaceType.PRIVATE,
            join_mode=JoinMode.INVITE_ONLY,
        )
    )
    r = SqliteSpaceRemoteMemberRepo(db)
    yield r
    await db.shutdown()


async def test_list_admin_instances_distinct_and_admin_only(repo):
    """Returns the DISTINCT instance_ids of remote members whose role is
    ADMIN — never MEMBER, and a household with two admins appears once."""
    await repo.add(
        space_id="sp1", instance_id="i-a", user_id="u1", user_pk=None, display_name=None
    )
    await repo.add(
        space_id="sp1", instance_id="i-a", user_id="u2", user_pk=None, display_name=None
    )
    await repo.add(
        space_id="sp1", instance_id="i-b", user_id="u3", user_pk=None, display_name=None
    )
    await repo.add(
        space_id="sp1", instance_id="i-c", user_id="u4", user_pk=None, display_name=None
    )
    # Two admins on i-a, one admin on i-b, i-c stays a plain member.
    await repo.set_role("sp1", "i-a", "u1", SpaceRole.ADMIN)
    await repo.set_role("sp1", "i-a", "u2", SpaceRole.ADMIN)
    await repo.set_role("sp1", "i-b", "u3", SpaceRole.ADMIN)

    admins = await repo.list_admin_instances("sp1")
    assert sorted(admins) == ["i-a", "i-b"]


async def test_list_admin_instances_empty_when_no_admins(repo):
    await repo.add(
        space_id="sp1", instance_id="i-a", user_id="u1", user_pk=None, display_name=None
    )
    assert await repo.list_admin_instances("sp1") == []
