"""Tests for SyncRtcSession (§4.2.3, §25.6.2)."""

from __future__ import annotations

import pytest

from socialhome.domain.federation import FederationEventType
from socialhome.federation.sync_rtc import (
    CHANNEL_LABEL,
    ICE_TIMEOUT_SECONDS,
    MAX_SIGNALING_SESSIONS,
    SyncRtcSession,
)


# ─── Constants ────────────────────────────────────────────────────────────


def test_channel_label_is_distinct_from_gfs():
    """sync-v1 must differ from the GFS gfs-v1 channel label."""
    assert CHANNEL_LABEL == "sync-v1"
    assert CHANNEL_LABEL != "gfs-v1"


def test_ice_timeout_default_matches_spec():
    """§4.2.3 says 15s timeout on ICE negotiation."""
    assert ICE_TIMEOUT_SECONDS == 15.0


def test_max_signaling_sessions_matches_audit_s8():
    """S-8 cap: 200 sessions per node."""
    assert MAX_SIGNALING_SESSIONS == 200


# ─── Construction guards ──────────────────────────────────────────────────


def test_invalid_sync_mode_rejected():
    """sync_mode is a formal field per S-16."""
    with pytest.raises(ValueError):
        SyncRtcSession(
            sync_id="sid",
            space_id="sp1",
            requester_instance_id="r",
            provider_instance_id="p",
            sync_mode="bogus",
        )


def test_invalid_role_rejected():
    """role must be provider or requester."""
    with pytest.raises(ValueError):
        SyncRtcSession(
            sync_id="sid",
            space_id="sp1",
            requester_instance_id="r",
            provider_instance_id="p",
            role="snoop",
        )


async def test_default_role_is_provider():
    s = SyncRtcSession(
        sync_id="sid",
        space_id="sp1",
        requester_instance_id="r",
        provider_instance_id="p",
    )
    assert s.role == "provider"
    assert s.sync_mode == "initial"


async def test_requester_id_is_a_formal_field_s14():
    """S-14: requester_instance_id is persisted on the session."""
    s = SyncRtcSession(
        sync_id="sid",
        space_id="sp1",
        requester_instance_id="alice-instance",
        provider_instance_id="bob-instance",
    )
    assert s.requester_instance_id == "alice-instance"
    assert s.provider_instance_id == "bob-instance"


# ─── S-13: create_answer vs set_answer ────────────────────────────────────


async def test_provider_create_offer_returns_sdp():
    s = SyncRtcSession(
        sync_id="sid1",
        space_id="sp1",
        requester_instance_id="r",
        provider_instance_id="p",
        role="provider",
    )
    sdp = await s.create_offer()
    assert sdp.startswith("v=0")
    assert isinstance(sdp, str)


async def test_provider_spawns_ice_drain_when_signaling_send_wired():
    """REGRESSION: pre-fix, ``SyncRtcSession`` never consumed
    ``pc.ice_candidates()`` outbound, so local trickle candidates
    never reached the peer and the DataChannel never opened. Wiring
    ``signaling_send`` arms the new ``_drain_ice`` task, which
    blocks on ``_local_description_applied`` until the OFFER is
    queued and then forwards every candidate via
    ``SPACE_SYNC_ICE``.

    We assert two narrow invariants:
      * Constructing with ``signaling_send=None`` parses + runs
        (back-compat — tests without a wired federation service).
      * Constructing with ``signaling_send=<callable>`` arms the
        ``_local_description_applied`` event guard that the drain
        loop awaits — and ``create_offer`` sets it so the drain
        can proceed.
    """
    s_none = SyncRtcSession(
        sync_id="sid-no-sig",
        space_id="sp1",
        requester_instance_id="r",
        provider_instance_id="p",
        role="provider",
        signaling_send=None,
    )
    assert s_none._signaling_send is None
    s_none.close()

    captured: list[tuple[FederationEventType, dict]] = []

    async def _sig(event_type, payload):
        captured.append((event_type, payload))

    s = SyncRtcSession(
        sync_id="sid2",
        space_id="sp1",
        requester_instance_id="r",
        provider_instance_id="p",
        role="provider",
        signaling_send=_sig,
    )
    assert s._signaling_send is _sig
    assert not s._local_description_applied.is_set()
    await s.create_offer()
    # create_offer must flip the local-description-applied event so
    # the drain loop stops waiting and can forward candidates.
    assert s._local_description_applied.is_set()
    s.close()


async def test_requester_cannot_create_offer_s13():
    s = SyncRtcSession(
        sync_id="sid",
        space_id="sp1",
        requester_instance_id="r",
        provider_instance_id="p",
        role="requester",
    )
    with pytest.raises(RuntimeError):
        await s.create_offer()


async def test_provider_cannot_create_answer_s13():
    """S-13: create_answer is a *requester* operation only."""
    s = SyncRtcSession(
        sync_id="sid",
        space_id="sp1",
        requester_instance_id="r",
        provider_instance_id="p",
        role="provider",
    )
    with pytest.raises(RuntimeError):
        await s.create_answer("v=0\r\n")


async def test_requester_create_answer_returns_distinct_sdp_s13():
    """S-13 fix: create_answer (requester) is NOT set_answer."""
    s = SyncRtcSession(
        sync_id="sid2",
        space_id="sp1",
        requester_instance_id="r",
        provider_instance_id="p",
        role="requester",
    )
    answer = await s.create_answer("v=0\r\no=- 0 0 IN IP4 1.2.3.4\r\n")
    assert answer.startswith("v=0")
    assert isinstance(answer, str)


async def test_requester_cannot_set_answer():
    s = SyncRtcSession(
        sync_id="sid",
        space_id="sp1",
        requester_instance_id="r",
        provider_instance_id="p",
        role="requester",
    )
    with pytest.raises(RuntimeError):
        await s.set_answer("v=0\r\n")


async def test_provider_set_answer_records_remote_sdp():
    s = SyncRtcSession(
        sync_id="sid",
        space_id="sp1",
        requester_instance_id="r",
        provider_instance_id="p",
        role="provider",
    )
    await s.create_offer()
    await s.set_answer("v=0\r\nthe-answer\r\n")
    assert s._remote_sdp == "v=0\r\nthe-answer\r\n"


async def test_set_answer_rejects_empty_sdp():
    s = SyncRtcSession(
        sync_id="sid",
        space_id="sp1",
        requester_instance_id="r",
        provider_instance_id="p",
        role="provider",
    )
    with pytest.raises(ValueError):
        await s.set_answer("")


async def test_create_answer_rejects_empty_sdp():
    s = SyncRtcSession(
        sync_id="sid",
        space_id="sp1",
        requester_instance_id="r",
        provider_instance_id="p",
        role="requester",
    )
    with pytest.raises(ValueError):
        await s.create_answer("")


# ─── ICE candidate handling ───────────────────────────────────────────────


async def test_add_ice_candidate_appends():
    s = SyncRtcSession(
        sync_id="sid",
        space_id="sp1",
        requester_instance_id="r",
        provider_instance_id="p",
    )
    await s.add_ice_candidate("candidate:1 1 UDP 2122252543 1.2.3.4 1234 typ host")
    assert len(s._ice_candidates) == 1


async def test_add_ice_candidate_rejects_empty():
    s = SyncRtcSession(
        sync_id="sid",
        space_id="sp1",
        requester_instance_id="r",
        provider_instance_id="p",
    )
    with pytest.raises(ValueError):
        await s.add_ice_candidate("")


# ─── Lifecycle ────────────────────────────────────────────────────────────


async def test_wait_ready_returns_false_in_stub_mode():
    """Without aiolibdatachannel, the channel never opens — caller must fall back."""
    s = SyncRtcSession(
        sync_id="sid",
        space_id="sp1",
        requester_instance_id="r",
        provider_instance_id="p",
    )
    ready = await s.wait_ready(timeout=0.05)
    assert ready is False


async def test_send_chunk_raises_when_channel_closed():
    s = SyncRtcSession(
        sync_id="sid",
        space_id="sp1",
        requester_instance_id="r",
        provider_instance_id="p",
    )
    s.close()
    with pytest.raises(ConnectionError):
        await s.send_chunk(b"data")


async def test_close_marks_session_closed():
    s = SyncRtcSession(
        sync_id="sid",
        space_id="sp1",
        requester_instance_id="r",
        provider_instance_id="p",
    )
    s.close()
    assert s.is_closed
    assert not s.is_ready
