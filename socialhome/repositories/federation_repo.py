"""Federation repository — peers, pairings, replay cache.

Covers the persistence surface the :class:`FederationService` depends on:

* CRUD for ``remote_instances`` rows — paired peers.
* Load + mark the in-memory :class:`ReplayCache` from the on-disk
  ``federation_replay_cache`` table.
* Helpers for ``pending_pairings`` (create / advance / drop) used by the
  pairing flow.
* Space instance bans (``space_instance_bans``) for the §13 moderation
  flow.

Kept separate from :mod:`outbox_repo` because the outbox has different
access patterns (a background retry loop, not the request/response path).
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..db import AsyncDatabase
from ..domain.federation import (
    InstanceSource,
    PairingSession,
    PairingStatus,
    RemoteInstance,
)
from .base import bool_col, row_to_dict, rows_to_dicts


@runtime_checkable
class AbstractFederationRepo(Protocol):
    # Remote instances ----------------------------------------------------
    async def get_instance(self, instance_id: str) -> RemoteInstance | None: ...
    async def get_instance_by_local_inbox_id(
        self, local_inbox_id: str
    ) -> RemoteInstance | None: ...
    async def save_instance(self, inst: RemoteInstance) -> RemoteInstance: ...
    async def set_proto_version(
        self,
        instance_id: str,
        proto_version: int,
    ) -> None: ...
    async def list_instances(
        self,
        *,
        source: str | None = None,
        status: str | None = None,
    ) -> list[RemoteInstance]: ...
    async def list_instances_in_space(self, space_id: str) -> list[RemoteInstance]: ...
    async def delete_instance(self, instance_id: str) -> None: ...
    async def mark_reachable(self, instance_id: str) -> None: ...
    async def mark_unreachable(self, instance_id: str) -> None: ...
    async def update_inbox(self, instance_id: str, new_url: str) -> None: ...
    async def update_alias(
        self,
        instance_id: str,
        alias: str | None,
    ) -> None: ...
    async def update_instance_home(
        self,
        instance_id: str,
        *,
        latitude: float,
        longitude: float,
    ) -> None: ...

    # Local instance identity (display_name + household coords) ----------
    async def get_local_identity(self) -> dict | None: ...

    # Replay cache --------------------------------------------------------
    async def load_replay_cache(
        self, within_hours: int = 1
    ) -> list[tuple[str, str]]: ...
    async def insert_replay_id(self, msg_id: str) -> None: ...
    async def prune_replay_cache(self, cutoff_iso: str) -> int: ...

    # Pairings ------------------------------------------------------------
    async def create_pairing(self, session: PairingSession) -> None: ...
    async def get_pairing(self, token: str) -> PairingSession | None: ...
    async def update_pairing(self, session: PairingSession) -> None: ...
    async def delete_pairing(self, token: str) -> None: ...
    async def cleanup_expired_pairings(self) -> int: ...

    # Bans ----------------------------------------------------------------
    async def ban_instance_from_space(
        self,
        space_id: str,
        instance_id: str,
        *,
        reason: str | None = None,
    ) -> None: ...
    async def is_instance_banned_from_space(
        self,
        space_id: str,
        instance_id: str,
    ) -> bool: ...


class SqliteFederationRepo:
    """SQLite-backed :class:`AbstractFederationRepo`."""

    def __init__(self, db: AsyncDatabase) -> None:
        self._db = db

    # ── Remote instances ───────────────────────────────────────────────

    async def get_instance(self, instance_id: str) -> RemoteInstance | None:
        row = await self._db.fetchone(
            "SELECT * FROM remote_instances WHERE id=?",
            (instance_id,),
        )
        return _row_to_instance(row_to_dict(row))

    async def get_instance_by_local_inbox_id(
        self,
        local_inbox_id: str,
    ) -> RemoteInstance | None:
        row = await self._db.fetchone(
            "SELECT * FROM remote_instances WHERE local_inbox_id=? LIMIT 1",
            (local_inbox_id,),
        )
        return _row_to_instance(row_to_dict(row))

    async def save_instance(self, inst: RemoteInstance) -> RemoteInstance:
        await self._db.enqueue(
            """
            INSERT INTO remote_instances(
                id, display_name, remote_identity_pk,
                key_self_to_remote, key_remote_to_self,
                remote_inbox_url, local_inbox_id,
                status, source, proto_version,
                remote_pq_algorithm, remote_pq_identity_pk, sig_suite,
                intro_relay_enabled, relay_via,
                home_lat, home_lon, paired_at, created_at,
                last_reachable_at, unreachable_since
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,COALESCE(?, datetime('now')),?,?)
            ON CONFLICT(id) DO UPDATE SET
                display_name=excluded.display_name,
                remote_identity_pk=excluded.remote_identity_pk,
                key_self_to_remote=excluded.key_self_to_remote,
                key_remote_to_self=excluded.key_remote_to_self,
                remote_inbox_url=excluded.remote_inbox_url,
                status=excluded.status,
                source=excluded.source,
                proto_version=excluded.proto_version,
                remote_pq_algorithm=excluded.remote_pq_algorithm,
                remote_pq_identity_pk=excluded.remote_pq_identity_pk,
                sig_suite=excluded.sig_suite,
                intro_relay_enabled=excluded.intro_relay_enabled,
                relay_via=excluded.relay_via,
                home_lat=excluded.home_lat,
                home_lon=excluded.home_lon,
                paired_at=excluded.paired_at,
                last_reachable_at=excluded.last_reachable_at,
                unreachable_since=excluded.unreachable_since
            """,
            (
                inst.id,
                inst.display_name,
                inst.remote_identity_pk,
                inst.key_self_to_remote,
                inst.key_remote_to_self,
                inst.remote_inbox_url,
                inst.local_inbox_id,
                inst.status.value,
                inst.source.value,
                inst.proto_version,
                inst.remote_pq_algorithm,
                inst.remote_pq_identity_pk,
                inst.sig_suite,
                int(inst.intro_relay_enabled),
                inst.relay_via,
                inst.home_lat,
                inst.home_lon,
                inst.paired_at,
                inst.created_at,
                inst.last_reachable_at,
                inst.unreachable_since,
            ),
        )
        return inst

    async def set_proto_version(
        self,
        instance_id: str,
        proto_version: int,
    ) -> None:
        """Apply the peer-advertised protocol version without touching
        identity, keys, or URL.

        Called from the inbound
        :data:`FederationEventType.INSTANCE_CAPABILITIES_UPDATED`
        handler. A single targeted UPDATE so the much larger
        :meth:`save_instance` (which re-writes every column) doesn't
        accidentally clobber state set elsewhere between reads.
        """
        await self._db.enqueue(
            "UPDATE remote_instances SET proto_version=? WHERE id=?",
            (proto_version, instance_id),
        )

    async def list_instances(
        self,
        *,
        source: str | None = None,
        status: str | None = None,
    ) -> list[RemoteInstance]:
        clauses: list[str] = []
        params: list = []
        if source is not None:
            clauses.append("source=?")
            params.append(source)
        if status is not None:
            clauses.append("status=?")
            params.append(status)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = await self._db.fetchall(
            f"SELECT * FROM remote_instances{where} ORDER BY display_name",
            tuple(params),
        )
        return [i for i in (_row_to_instance(d) for d in rows_to_dicts(rows)) if i]

    async def list_instances_in_space(
        self,
        space_id: str,
    ) -> list[RemoteInstance]:
        """Confirmed peers that are members of ``space_id`` and not
        instance-banned from it (§24.11)."""
        rows = await self._db.fetchall(
            """
            SELECT ri.* FROM remote_instances ri
            JOIN space_instances si ON si.instance_id = ri.id
            WHERE si.space_id = ?
              AND ri.status = ?
              AND NOT EXISTS (
                  SELECT 1 FROM space_instance_bans sib
                  WHERE sib.space_id = si.space_id
                    AND sib.instance_id = ri.id
              )
            ORDER BY ri.display_name
            """,
            (space_id, PairingStatus.CONFIRMED.value),
        )
        return [i for i in (_row_to_instance(d) for d in rows_to_dicts(rows)) if i]

    async def delete_instance(self, instance_id: str) -> None:
        await self._db.enqueue(
            "DELETE FROM remote_instances WHERE id=?",
            (instance_id,),
        )

    async def mark_reachable(self, instance_id: str) -> None:
        await self._db.enqueue(
            "UPDATE remote_instances SET last_reachable_at=datetime('now'), "
            "unreachable_since=NULL WHERE id=?",
            (instance_id,),
        )

    async def mark_unreachable(self, instance_id: str) -> None:
        await self._db.enqueue(
            "UPDATE remote_instances "
            "SET unreachable_since=COALESCE(unreachable_since, datetime('now')) "
            "WHERE id=?",
            (instance_id,),
        )

    async def update_inbox(self, instance_id: str, new_url: str) -> None:
        await self._db.enqueue(
            "UPDATE remote_instances SET remote_inbox_url=? WHERE id=?",
            (new_url, instance_id),
        )

    async def update_alias(
        self,
        instance_id: str,
        alias: str | None,
    ) -> None:
        """Set or clear the local alias for a paired peer.

        ``alias`` is the local-only display string the admin set in
        the UI ("Brother's house"). ``None`` clears it so the SPA
        falls back to the peer's federated display_name. Never
        federated — purely local UX state.
        """
        await self._db.enqueue(
            "UPDATE remote_instances SET local_alias=? WHERE id=?",
            (alias, instance_id),
        )

    async def update_instance_home(
        self,
        instance_id: str,
        *,
        latitude: float,
        longitude: float,
    ) -> None:
        """Update the peer's ``home_lat`` / ``home_lon`` columns only.

        Used by the inbound
        :data:`FederationEventType.LOCAL_HOME_LOCATION_CHANGED`
        handler. Values are 4dp-truncated on write per §25 — the
        inbound caller has already validated the body but we defend-
        in-depth here. Missing rows are silent no-ops (the §24.11
        inbound pipeline has already verified the sender is known).
        """
        await self._db.enqueue(
            "UPDATE remote_instances SET home_lat = ?, home_lon = ? WHERE id = ?",
            (
                round(float(latitude), 4),
                round(float(longitude), 4),
                instance_id,
            ),
        )

    async def get_local_identity(self) -> dict | None:
        """Return the local instance's display_name + household coords.

        Used by routes that need a friendly name + map pin for "us" —
        the keys + secrets in this row stay private. Returns ``None``
        before bootstrap creates the row (only possible during a very
        early test fixture).
        """
        row = await self._db.fetchone(
            "SELECT instance_id, display_name, home_lat, home_lon "
            "FROM instance_identity WHERE id='self'",
        )
        if row is None:
            return None
        return {
            "instance_id": row["instance_id"],
            "display_name": row["display_name"],
            "home_lat": row["home_lat"],
            "home_lon": row["home_lon"],
        }

    # ── Replay cache ───────────────────────────────────────────────────

    async def load_replay_cache(
        self,
        within_hours: int = 1,
    ) -> list[tuple[str, str]]:
        rows = await self._db.fetchall(
            "SELECT msg_id, received_at FROM federation_replay_cache "
            "WHERE received_at > datetime('now', ?)",
            (f"-{within_hours} hours",),
        )
        return [(r["msg_id"], r["received_at"]) for r in rows]

    async def insert_replay_id(self, msg_id: str) -> None:
        await self._db.enqueue(
            "INSERT OR IGNORE INTO federation_replay_cache(msg_id) VALUES(?)",
            (msg_id,),
        )

    async def prune_replay_cache(self, cutoff_iso: str) -> int:
        """Delete replay entries older than ``cutoff_iso``.

        Returns the count purged. Callers typically run this from an hourly
        scheduler.
        """
        before = await self._db.fetchval(
            "SELECT COUNT(*) FROM federation_replay_cache WHERE received_at < ?",
            (cutoff_iso,),
            default=0,
        )
        await self._db.enqueue(
            "DELETE FROM federation_replay_cache WHERE received_at < ?",
            (cutoff_iso,),
        )
        return int(before)

    # ── Pairings ───────────────────────────────────────────────────────

    async def create_pairing(self, session: PairingSession) -> None:
        await self._db.enqueue(
            """
            INSERT INTO pending_pairings(
                token, own_identity_pk, own_dh_pk, own_dh_sk,
                peer_identity_pk, peer_dh_pk, peer_inbox_url, inbox_url,
                own_local_inbox_id,
                verification_code, intro_note, relay_via,
                status, issued_at, expires_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                session.token,
                session.own_identity_pk,
                session.own_dh_pk,
                session.own_dh_sk,
                session.peer_identity_pk,
                session.peer_dh_pk,
                session.peer_inbox_url,
                session.inbox_url,
                session.own_local_inbox_id,
                session.verification_code,
                session.intro_note,
                session.relay_via,
                session.status.value,
                session.issued_at,
                session.expires_at,
            ),
        )

    async def get_pairing(self, token: str) -> PairingSession | None:
        row = await self._db.fetchone(
            "SELECT * FROM pending_pairings WHERE token=?",
            (token,),
        )
        d = row_to_dict(row)
        if d is None:
            return None
        return PairingSession(
            token=d["token"],
            own_identity_pk=d["own_identity_pk"],
            own_dh_pk=d["own_dh_pk"],
            own_dh_sk=d["own_dh_sk"],
            peer_identity_pk=d.get("peer_identity_pk"),
            peer_dh_pk=d.get("peer_dh_pk"),
            peer_inbox_url=d.get("peer_inbox_url"),
            inbox_url=d["inbox_url"],
            own_local_inbox_id=d["own_local_inbox_id"],
            verification_code=d.get("verification_code"),
            intro_note=d.get("intro_note"),
            relay_via=d.get("relay_via"),
            status=PairingStatus(d.get("status", "pending_sent")),
            issued_at=d.get("issued_at"),
            expires_at=d.get("expires_at"),
        )

    async def update_pairing(self, session: PairingSession) -> None:
        await self._db.enqueue(
            """
            UPDATE pending_pairings SET
                peer_identity_pk=?,
                peer_dh_pk=?,
                peer_inbox_url=?,
                verification_code=?,
                intro_note=?,
                relay_via=?,
                status=?
            WHERE token=?
            """,
            (
                session.peer_identity_pk,
                session.peer_dh_pk,
                session.peer_inbox_url,
                session.verification_code,
                session.intro_note,
                session.relay_via,
                session.status.value,
                session.token,
            ),
        )

    async def delete_pairing(self, token: str) -> None:
        await self._db.enqueue(
            "DELETE FROM pending_pairings WHERE token=?",
            (token,),
        )

    async def cleanup_expired_pairings(self) -> int:
        """Delete ``pending_pairings`` past their ``expires_at`` plus the
        orphan ``remote_instances`` rows (in PENDING_SENT /
        PENDING_RECEIVED status) whose ``local_inbox_id`` matched one
        of those sessions. Returns the count of pairing rows pruned.

        SQLite's ``datetime()`` function normalises both the
        ``"YYYY-MM-DDTHH:MM:SS+00:00"`` ISO 8601 shape Python's
        :meth:`datetime.isoformat` produces and the
        ``"YYYY-MM-DD HH:MM:SS"`` shape SQLite's ``datetime('now')``
        emits, so a single comparison works regardless of which path
        wrote the row.

        Already-confirmed pairs are safe: :meth:`confirm` deletes the
        ``pending_pairings`` row before flipping the
        ``remote_instances`` row to ``CONFIRMED``, so no expired
        session ever points at a confirmed instance.
        """
        expired_count = await self._db.fetchval(
            "SELECT COUNT(*) FROM pending_pairings "
            "WHERE datetime(expires_at) < datetime('now')",
            default=0,
        )
        if not expired_count:
            return 0
        await self._db.enqueue(
            """
            DELETE FROM remote_instances
            WHERE status IN ('pending_sent', 'pending_received')
              AND local_inbox_id IN (
                SELECT own_local_inbox_id FROM pending_pairings
                WHERE datetime(expires_at) < datetime('now')
              )
            """,
        )
        await self._db.enqueue(
            "DELETE FROM pending_pairings WHERE datetime(expires_at) < datetime('now')",
        )
        return int(expired_count)

    # ── Instance bans ──────────────────────────────────────────────────

    async def ban_instance_from_space(
        self,
        space_id: str,
        instance_id: str,
        *,
        reason: str | None = None,
    ) -> None:
        await self._db.enqueue(
            """
            INSERT INTO space_instance_bans(space_id, instance_id, reason)
            VALUES(?, ?, ?)
            ON CONFLICT(space_id, instance_id) DO UPDATE SET
                reason=excluded.reason
            """,
            (space_id, instance_id, reason),
        )

    async def is_instance_banned_from_space(
        self,
        space_id: str,
        instance_id: str,
    ) -> bool:
        row = await self._db.fetchone(
            "SELECT 1 FROM space_instance_bans WHERE space_id=? AND instance_id=?",
            (space_id, instance_id),
        )
        return row is not None


# ─── Row → domain helper ──────────────────────────────────────────────────


def _row_to_instance(row: dict | None) -> RemoteInstance | None:
    if row is None:
        return None
    return RemoteInstance(
        id=row["id"],
        display_name=row["display_name"],
        remote_identity_pk=row["remote_identity_pk"],
        key_self_to_remote=row["key_self_to_remote"],
        key_remote_to_self=row["key_remote_to_self"],
        remote_inbox_url=row["remote_inbox_url"],
        local_inbox_id=row["local_inbox_id"],
        status=PairingStatus(row.get("status", "confirmed")),
        source=InstanceSource(row.get("source", "manual")),
        proto_version=int(row.get("proto_version") or 1),
        remote_pq_algorithm=row.get("remote_pq_algorithm"),
        remote_pq_identity_pk=row.get("remote_pq_identity_pk"),
        sig_suite=str(row.get("sig_suite") or "ed25519"),
        intro_relay_enabled=bool_col(row.get("intro_relay_enabled", 1)),
        relay_via=row.get("relay_via"),
        home_lat=row.get("home_lat"),
        home_lon=row.get("home_lon"),
        paired_at=row.get("paired_at"),
        created_at=row.get("created_at"),
        last_reachable_at=row.get("last_reachable_at"),
        unreachable_since=row.get("unreachable_since"),
        local_alias=row.get("local_alias"),
    )
