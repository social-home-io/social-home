"""Tests for the shared visibility helper."""

from __future__ import annotations


from socialhome.services.visibility import hidden_for_peer


class _FakeRepo:
    def __init__(self, mapping: dict[str, set[str]]) -> None:
        self._mapping = mapping

    async def hidden_user_ids_for_peer(self, peer_id: str) -> frozenset[str]:
        return frozenset(self._mapping.get(peer_id, set()))


class _BrokenRepo:
    async def hidden_user_ids_for_peer(self, peer_id: str) -> frozenset[str]:
        raise RuntimeError("db went away")


async def test_hidden_for_peer_returns_set_for_known_peer():
    repo = _FakeRepo({"peer-1": {"u-alice", "u-bob"}})
    assert await hidden_for_peer(repo, "peer-1") == {"u-alice", "u-bob"}


async def test_hidden_for_peer_returns_empty_for_unknown_peer():
    repo = _FakeRepo({"peer-1": {"u-alice"}})
    assert await hidden_for_peer(repo, "peer-2") == set()


async def test_hidden_for_peer_returns_empty_when_repo_is_none():
    """Back-compat: tests that don't wire the repo default to visible."""
    assert await hidden_for_peer(None, "peer-anything") == set()


async def test_hidden_for_peer_swallows_repo_errors_and_defaults_visible():
    """Fail-soft: a transient repo error must not block federation
    outbound. Default-visible matches the existing
    ``ProfileFederationOutbound`` behaviour."""
    assert await hidden_for_peer(_BrokenRepo(), "peer-1") == set()
