"""AppFederationService — cross-household Social Home Apps federation bridge.

Translates between the federation layer (inbound events + binary frames from
``fed-app-v1``) and the SPA layer (WebSocket pushes to local users).

Security invariants
-------------------
* ``_require_enabled`` gates every outbound — an uninstalled or disabled app
  cannot initiate or send federation messages.
* Inbound delivery is fail-soft: if the app is not installed or not enabled,
  the event is silently dropped after a debug log (no error to the peer).
* No application payload ever appears in plaintext in a federation envelope.
  Both the binary and JSON paths rely on :class:`FederationService` for
  encryption; this service only passes dicts down to the send methods and
  never touches the wire format.

The ``session_id`` returned by :meth:`open_session` is the de-facto game /
whiteboard / mini-app session namespace.  All messages carry it so the SPA can
dispatch to the correct in-page component without any server-side state.

Person-addressed sessions: :meth:`open_session` targets a specific person
(local or remote).  A local-loopback open delivers a ``kind="session"`` frame
straight to the target and initiator over WebSocket (no federation send); a
remote open sends an ``APP_SESSION`` event, carrying ``to_user`` / ``from_user``
to v_18+ peers (gated on
:data:`FederationCapability.MIN_FOR_APP_USER_ROUTING`) so the peer can route to
the addressed person.  Inbound ``APP_MESSAGE`` delivery still fans out to every
local user whose WebSocket is open (per-user inbound routing is a follow-up);
the app's ``session_id`` scopes the semantics for the SPA.
"""

from __future__ import annotations

import logging
from uuid import uuid4
from typing import TYPE_CHECKING, Any

from ..domain.apps import AppAgeRestrictedError, AppNotEnabledError, AppNotFoundError
from ..domain.federation import FederationEvent, FederationEventType, PairingStatus
from ..domain.federation_capabilities import FederationCapability

if TYPE_CHECKING:
    from ..repositories.app_repo import AbstractAppRepo
    from ..repositories.cp_repo import AbstractCpRepo
    from ..repositories.federation_repo import AbstractFederationRepo
    from ..repositories.user_repo import AbstractUserRepo
    from ..infrastructure.ws_manager import WebSocketManager

log = logging.getLogger(__name__)


class AppFederationService:
    """Bridge between the federation layer and Social Home Apps.

    Constructor parameters are injected; no I/O of its own beyond what the
    injected services provide.

    Parameters
    ----------
    app_repo:
        Installed-apps registry — used to validate that an app exists
        and is enabled before any outbound or inbound delivery.
    user_repo:
        Local user directory — used to enumerate user ids for WebSocket
        fan-out on inbound messages.
    ws:
        WebSocket manager — delivers ``app.message`` frames to all open
        connections.
    federation:
        The core :class:`FederationService` — used for outbound sends
        (``send_event`` for session control, ``send_app_message`` for
        data messages).
    federation_repo:
        Federation peer directory — used by :meth:`list_peers`.
    """

    __slots__ = (
        "_app_repo",
        "_user_repo",
        "_ws",
        "_federation",
        "_federation_repo",
        "_cp_repo",
    )

    def __init__(
        self,
        *,
        app_repo: AbstractAppRepo,
        user_repo: AbstractUserRepo,
        ws: WebSocketManager,
        federation: Any,
        federation_repo: AbstractFederationRepo,
        cp_repo: "AbstractCpRepo | None" = None,
    ) -> None:
        self._app_repo = app_repo
        self._user_repo = user_repo
        self._ws = ws
        self._federation = federation
        self._federation_repo = federation_repo
        self._cp_repo = cp_repo

    # ─── Public API ───────────────────────────────────────────────────────────

    async def list_peers(self) -> list[dict]:
        """Return confirmed instances as ``[{instance_id, display_name}]``.

        The SPA uses this to populate the peer picker when an app wants
        to start a cross-household session.
        """
        instances = await self._federation_repo.list_instances(
            status=PairingStatus.CONFIRMED.value,
        )
        return [
            {
                "instance_id": inst.id,
                "display_name": inst.effective_display_name,
            }
            for inst in instances
        ]

    async def list_contacts(self, *, self_user_id: str) -> list[dict]:
        """Return the set of people a user can challenge to an app session.

        This is the same roster that DMs and the ``/friends`` page expose:
        members of paired households, minus personal blocks.  The per-peer
        hide-list is applied upstream at pairing time, so hidden members
        never reach ``remote_users`` in the first place.

        Includes:
        * All active local household members (excluding the caller).
        * All known remote users across every paired household.

        Blocked contacts are excluded using the same mechanism as
        ``/friends``: ``list_blocked(self_user_id)`` returns
        ``[(blocked_user_id, blocked_at), ...]`` and users whose
        ``user_id`` is in that set are dropped from both the local and
        remote populations.

        Each contact is a dict with keys:
        ``instance_id``, ``user_ref``, ``display_name``, ``is_local``,
        ``online``.

        Remote online presence is not yet wired in — ``online`` is always
        ``False`` for remote contacts (deferred to a future wiring task).
        """
        own = self._federation.own_instance_id
        connected = self._ws.connected_users()

        # Personal blocks (§Privacy) — same filter as /friends and DM roster.
        blocked_ids = {
            uid for uid, _ in await self._user_repo.list_blocked(self_user_id)
        }

        out: list[dict] = []

        for u in await self._user_repo.list_all():
            if u.user_id == self_user_id:
                continue
            if u.user_id in blocked_ids:
                continue
            out.append(
                {
                    "instance_id": own,
                    "user_ref": u.user_id,
                    "display_name": u.display_name,
                    "is_local": True,
                    "online": u.user_id in connected,
                }
            )

        for r in await self._user_repo.list_all_known_remote():
            if r.user_id in blocked_ids:
                continue
            out.append(
                {
                    "instance_id": r.instance_id,
                    "user_ref": r.remote_username,
                    "display_name": r.display_name or r.remote_username,
                    "is_local": False,
                    "online": await self._remote_online(
                        r.instance_id, r.remote_username
                    ),
                }
            )

        return out

    async def open_session(
        self,
        *,
        app_id: str,
        target: dict,
        actor_user_id: str,
    ) -> str:
        """Open an app session with a specific *person* (local or remote).

        ``target`` is ``{"instance_id": str, "user_ref": str, "is_local":
        bool}`` as returned by :meth:`list_contacts`:

        * ``is_local`` — the target lives on this household; ``user_ref`` is
          a local ``user_id``.  We deliver the ``kind="session"`` open frame
          straight to the target *and* the initiator over WebSocket — no
          federation send, no fan-out to every local user.
        * remote — ``instance_id`` is the target household and ``user_ref``
          is the target's remote username on that household.  We send an
          ``APP_SESSION`` event there.

        Validates that the app is installed and enabled and that the actor
        passes the age gate, allocates a ``session_id``, and returns it so
        the caller can hand it back to the SPA.

        §FIX-I2 (relaxed, proto v_18): per-user identity on the wire
        (``to_user`` / ``from_user``) is now permitted because the
        challengeable roster is exactly the pairing-scoped DM / ``/friends``
        set — a consensual, not covert, audience.  ``to_user`` lets the peer
        route the open to the addressed person instead of fanning to every
        local user; ``from_user`` is the initiator's username so the
        recipient can show who challenged them.  Both fields are gated on
        :data:`FederationCapability.MIN_FOR_APP_USER_ROUTING` and are omitted
        for sub-v_18 peers, which fall back to the legacy household-addressed
        fan-out (no ``to_user``).  Session control always rides the JSON
        event path regardless of peer version — the binary channel is only
        used for :meth:`send_message`.
        """
        await self._require_enabled(app_id)
        await self._assert_age_allowed(app_id, actor_user_id)
        session_id = uuid4().hex
        own = self._federation.own_instance_id

        if target.get("is_local"):
            # Local loopback — deliver the open frame to the addressed person
            # and the initiator only, never a fan-out to all local users.
            recipients = await self._age_filter_recipients(
                app_id, [target["user_ref"], actor_user_id]
            )
            await self._emit_session_open(
                recipients,
                app_id=app_id,
                session_id=session_id,
                from_instance=own,
                from_user=actor_user_id,
            )
            return session_id

        # Remote — address the peer household; include per-user routing fields
        # only when the peer is v_18+ (else legacy household fan-out).
        payload: dict[str, Any] = {
            "app_id": app_id,
            "session_id": session_id,
            "verb": "open",
        }
        supported = await self._federation.peer_supports(
            target["instance_id"],
            min_version=FederationCapability.MIN_FOR_APP_USER_ROUTING,
        )
        if supported:
            me = await self._user_repo.get_by_user_id(actor_user_id)
            from_user = me.username if me is not None else actor_user_id
            payload["to_user"] = target["user_ref"]
            payload["from_user"] = from_user
        await self._federation.send_event(
            to_instance_id=target["instance_id"],
            event_type=FederationEventType.APP_SESSION,
            payload=payload,
        )
        return session_id

    async def send_message(
        self,
        *,
        app_id: str,
        session_id: str,
        peer_instance_id: str,
        payload: dict,
        actor_user_id: str,
    ) -> None:
        """Send an app-layer message to a peer household.

        Validates that the app is installed and enabled, then delegates to
        :meth:`FederationService.send_app_message` which selects the binary
        ``fed-app-v1`` channel (v_17+ confirmed peers) or falls back to the
        JSON ``APP_MESSAGE`` federation event path — in both cases the
        ``payload`` dict is AES-256-GCM-sealed and never sent in plaintext.

        ``actor_user_id`` is recorded for audit / rate-limiting hooks; it is
        not forwarded in the current v1 wire shape.
        """
        await self._require_enabled(app_id)
        await self._assert_age_allowed(app_id, actor_user_id)
        await self._federation.send_app_message(
            to_instance_id=peer_instance_id,
            app_id=app_id,
            session_id=session_id,
            payload=payload,
        )

    # ─── Inbound federation dispatch ─────────────────────────────────────────

    async def on_inbound_event(self, event: FederationEvent) -> None:
        """Handle an inbound ``APP_SESSION`` or ``APP_MESSAGE`` JSON event.

        Called by the :class:`FederationService` event registry after the
        §24.11 pipeline has validated and decrypted the envelope.  Extracts
        the app-specific fields and fans out via :meth:`_deliver`.
        """
        if not isinstance(event.payload, dict):
            log.debug("app_federation: inbound event with non-dict payload — dropping")
            return
        app_id = event.payload.get("app_id")
        session_id = event.payload.get("session_id")
        if not app_id or not session_id:
            log.debug(
                "app_federation: inbound event missing app_id/session_id — dropping"
            )
            return

        if event.event_type is FederationEventType.APP_SESSION:
            # Pass the whole payload as the session info dict.
            app_payload: dict = dict(event.payload)
            kind = "session"
        else:
            # APP_MESSAGE: the application data is nested under "data".
            app_payload = event.payload.get("data") or {}
            kind = "message"

        await self._deliver(
            app_id,
            session_id,
            from_instance=event.from_instance,
            payload=app_payload,
            kind=kind,
        )

    async def on_inbound_message(
        self,
        instance_id: str,
        app_id: str,
        session_id: str,
        payload: dict,
    ) -> None:
        """Handle an inbound binary app frame from ``fed-app-v1``.

        Called by :meth:`FederationService._app_inbound_handler` after the
        §24.11 pipeline validates the envelope and the payload is decrypted.
        """
        await self._deliver(
            app_id,
            session_id,
            from_instance=instance_id,
            payload=payload,
            kind="message",
        )

    # ─── Internal helpers ─────────────────────────────────────────────────────

    async def _remote_online(self, instance_id: str, remote_username: str) -> bool:
        """Return whether a remote user has a live session.

        Remote presence is not yet wired into this service — always returns
        ``False`` (fail-soft).  A future task will plumb the presence service
        so cross-household online state can be surfaced in the contact picker.
        """
        _ = instance_id, remote_username  # reserved for future wiring
        return False

    async def _require_enabled(self, app_id: str) -> None:
        """Raise :class:`AppNotFoundError` or :class:`AppNotEnabledError`."""
        app = await self._app_repo.get(app_id)
        if app is None:
            raise AppNotFoundError(app_id)
        if not app.enabled:
            raise AppNotEnabledError(app_id)

    async def _assert_age_allowed(self, app_id: str, actor_user_id: str) -> None:
        """Raise :class:`AppAgeRestrictedError` for protected minors below min_age.

        Fail-closed: if the app is not found or has no age restriction,
        this is a no-op.  Only blocks when the app exists, has a min_age > 0,
        cp_repo is configured, and the user's protection record shows a
        declared_age below the threshold.
        """
        if self._cp_repo is None:
            return
        app = await self._app_repo.get(app_id)
        if app is None or app.min_age <= 0:
            return
        p = await self._cp_repo.get_user_protection(actor_user_id)
        if p is None:
            return
        if not p.get("child_protection_enabled"):
            return
        declared = int(p.get("declared_age") or 0)
        if declared < app.min_age:
            raise AppAgeRestrictedError(
                f"This app is restricted to ages {app.min_age}+."
            )

    async def _emit_session_open(
        self,
        recipient_user_ids: list[str],
        *,
        app_id: str,
        session_id: str,
        from_instance: str,
        from_user: str,
    ) -> None:
        """Push a ``kind="session"`` open frame to each recipient.

        Builds the frame once and delivers it per-user via
        :meth:`WebSocketManager.broadcast_to_user` — never a fan-out to all
        local users.  ``from_user`` rides the frame so the recipient's SPA
        can show who initiated the session.  Reused by both the local
        loopback path and (Task 6) inbound person-addressed delivery.
        """
        frame = {
            "type": "app.message",
            "app_id": app_id,
            "session_id": session_id,
            "from_instance": from_instance,
            "kind": "session",
            "from_user": from_user,
            "payload": {
                "app_id": app_id,
                "session_id": session_id,
                "verb": "open",
            },
        }
        for uid in recipient_user_ids:
            await self._ws.broadcast_to_user(uid, frame)

    async def _age_filter_recipients(
        self, app_id: str, user_ids: list[str]
    ) -> list[str]:
        """Drop protected minors below the app's ``min_age`` from ``user_ids``.

        Fast path returns the input unchanged when the app has no age
        restriction or no ``cp_repo`` is configured.  Preserves order and
        de-duplicates while keeping first occurrence.
        """
        # De-dupe preserving order.
        seen: set[str] = set()
        ordered: list[str] = []
        for uid in user_ids:
            if uid not in seen:
                seen.add(uid)
                ordered.append(uid)

        app = await self._app_repo.get(app_id)
        if app is None or app.min_age <= 0 or self._cp_repo is None:
            return ordered

        allowed: list[str] = []
        for uid in ordered:
            p = await self._cp_repo.get_user_protection(uid)
            if p is None or not p.get("child_protection_enabled"):
                allowed.append(uid)
                continue
            declared = int(p.get("declared_age") or 0)
            if declared >= app.min_age:
                allowed.append(uid)
        return allowed

    async def _deliver(
        self,
        app_id: str,
        session_id: str,
        *,
        from_instance: str,
        payload: dict,
        kind: str,
    ) -> None:
        """Fan an app message out to all local users via WebSocket.

        Silently drops (debug log) if the app is not installed or disabled —
        the peer doesn't need to know we don't have this app.

        Parameters
        ----------
        kind:
            ``"session"`` for ``APP_SESSION`` control events,
            ``"message"`` for ``APP_MESSAGE`` and binary ``fed-app-v1``
            data frames.  Forwarded to the SPA so the bridge can relay
            it into the iframe, letting apps distinguish invites from
            in-game moves and route by session.
        """
        app = await self._app_repo.get(app_id)
        if app is None or not app.enabled:
            log.debug(
                "app_federation: dropping inbound for app %r (not installed/enabled)",
                app_id,
            )
            return
        users = await self._user_repo.list_all()
        user_ids = [u.user_id for u in users]
        if not user_ids:
            return

        # Age-gate filter: skip recipients who are protected minors below
        # the app's minimum age.  Fast path when the app has no restriction.
        user_ids = await self._age_filter_recipients(app_id, user_ids)

        if not user_ids:
            return

        await self._ws.broadcast_to_users(
            user_ids,
            {
                "type": "app.message",
                "app_id": app_id,
                "session_id": session_id,
                "from_instance": from_instance,
                "kind": kind,
                "payload": payload,
            },
        )
