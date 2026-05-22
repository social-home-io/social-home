"""Federation service — outbound delivery and inbound validation (§11–§13, §24.11).

This module owns two responsibilities:

A) **Outbound**: encrypt, sign, and POST federation events to paired peer
   instances using the per-pair directional session keys.

B) **Inbound**: run the §24.11 validation pipeline on received inbox
   bodies, then dispatch validated events to the in-process EventBus.

Pairing helpers (``initiate_pairing``, ``accept_pairing``,
``confirm_pairing``) drive the §11 QR-code handshake to establish the
shared session keys and ``RemoteInstance`` row.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

import aiohttp
import asyncio
import orjson as _orjson

if TYPE_CHECKING:
    from .route_discovery import RouteDiscoveryService
    from .routed_envelope import SpaceRoutedHandler

from ..crypto import (
    ReplayCache,
)
from ..db import AsyncDatabase
from ..domain.events import (
    ConnectionReachable,
    LocalHomeLocationUpdated,
    PairingIntroRelayReceived,
    PeerHomeChanged,
    SpaceConfigChanged,
)
from ..domain.federation_capabilities import FederationCapability
from ..domain.media_validator import validate_inbound_media_meta
from ..domain.federation import (
    BroadcastResult,
    DeliveryResult,
    FederationEvent,
    FederationEventType,
    PairingStatus,
    RemoteInstance,
)
from ..infrastructure.event_bus import EventBus
from ..infrastructure.key_manager import KeyManager
from ..repositories.federation_repo import AbstractFederationRepo
from ..repositories.outbox_repo import AbstractOutboxRepo
from .encoder import FederationEncoder
from .pq_signer import PqSigner
from .inbound_validator import (
    InboundContext,
    InboundPipeline,
    _InboxInstance,
    make_ban_check,
    make_check_deprovisioned_author,
    make_check_replay,
    make_check_timestamp,
    make_decrypt_and_parse,
    make_idempotency_check,
    make_lookup_instance,
    make_lookup_instance_by_id,
    make_parse_json,
    make_persist_replay,
    make_verify_signature,
)
from .pairing_coordinator import PairingCoordinator
from .peer_pairing_client import PeerPairingClient
from .event_dispatch_registry import EventDispatchRegistry


def _dumps(obj: dict) -> str:
    """Compact UTF-8 JSON — the wire format for every federation envelope."""
    return _orjson.dumps(obj).decode("utf-8")


def _loads(s: str | bytes) -> dict:
    return _orjson.loads(s)


log = logging.getLogger(__name__)

#: Maximum allowed clock skew for inbound envelopes (§24.11 §5).
_TIMESTAMP_SKEW_SECONDS = 300

#: Pairing QR token lifetime.
_PAIRING_TTL_SECONDS = 300

#: Length of the SAS verification code (digits).
_SAS_DIGITS = 6


class FederationService:
    """Core federation handler — outbound delivery and inbound dispatch.

    All constructor parameters are injected; the service has no I/O of its
    own beyond the HTTP client it uses to POST to peer inbox URLs.

    Parameters
    ----------
    db:
        The application database (used for replay cache persistence).
    federation_repo:
        Abstracts ``remote_instances``, replay cache, and pairing rows.
    outbox_repo:
        Abstracts ``federation_outbox`` for reliable at-least-once delivery.
    key_manager:
        KEK-based encrypt/decrypt for session keys at rest.
    bus:
        In-process domain event bus.
    own_instance_id:
        This instance's stable identifier (derived from ``own_identity_pk``).
    own_identity_seed:
        32-byte Ed25519 private key seed for signing outbound envelopes.
    own_identity_pk:
        32-byte Ed25519 public key corresponding to ``own_identity_seed``.
    http_client:
        Optional aiohttp ``ClientSession``-compatible object. When ``None``
        the service creates a session on first use. Injectable for testing.
    """

    __slots__ = (
        "_db",
        "_federation_repo",
        "_outbox_repo",
        "_key_manager",
        "_bus",
        "_own_instance_id",
        "_own_identity_seed",
        "_own_identity_pk",
        "_own_pq_seed",
        "_own_pq_pk",
        "_sig_suite",
        "_http_client",
        "_replay_cache",
        "_sync_manager",
        "_call_signaling",
        "_ice_servers",
        "_idempotency_cache",
        "_typing_service",
        "_dm_routing_service",
        "_transport",
        "_presence_service",
        "_online_status_service",
        "_space_sync_service",
        "_space_sync_receiver",
        "_encoder",
        "_pairing",
        "_inbound_pipeline",
        "_rtc_inbound_pipeline",
        "_event_registry",
        "_gfs_connection_service",
        "_user_repo",
        "_route_service",
        "_routed_handler",
    )

    def __init__(
        self,
        db: AsyncDatabase,
        federation_repo: AbstractFederationRepo,
        outbox_repo: AbstractOutboxRepo,
        key_manager: KeyManager,
        bus: EventBus,
        own_instance_id: str,
        own_identity_seed: bytes,
        own_identity_pk: bytes,
        http_client=None,
        sync_manager=None,
        call_signaling=None,
        ice_servers: list[dict] | None = None,
        own_pq_seed: bytes | None = None,
        own_pq_pk: bytes | None = None,
        sig_suite: str = "ed25519",
    ) -> None:
        self._db = db
        self._federation_repo = federation_repo
        self._outbox_repo = outbox_repo
        self._key_manager = key_manager
        self._bus = bus
        self._own_instance_id = own_instance_id
        self._own_identity_seed = own_identity_seed
        self._own_identity_pk = own_identity_pk
        self._own_pq_seed = own_pq_seed
        self._own_pq_pk = own_pq_pk
        self._sig_suite = sig_suite
        self._http_client = http_client
        self._replay_cache = ReplayCache(window=timedelta(hours=1))
        self._sync_manager = sync_manager
        self._call_signaling = call_signaling
        self._ice_servers = ice_servers or []
        self._idempotency_cache = None
        self._typing_service = None
        self._dm_routing_service = None
        self._transport = None
        self._presence_service = None
        self._online_status_service = None
        self._space_sync_service = None
        self._space_sync_receiver = None
        self._gfs_connection_service = None
        # Set via :meth:`attach_user_repo` after construction. Enables
        # the receiver-side deprovisioned-author filter (§24.11 step 11).
        # When unset, the filter step is omitted from the pipeline — a
        # legacy boot path / unit-test fixture without a user repo still
        # functions, just without the visibility backstop.
        self._user_repo = None
        # §D2 PR2 mesh-routing primitives — set via :meth:`attach_mesh`.
        # When wired, :meth:`send_with_mesh_fallback` discovers a path
        # through the federation graph and ships SPACE_ROUTED when a
        # peer isn't directly CONFIRMED. Without them, the helper just
        # surfaces a failed DeliveryResult for non-CONFIRMED peers.
        self._route_service: RouteDiscoveryService | None = None
        self._routed_handler: SpaceRoutedHandler | None = None
        # Envelope crypto delegate (encrypt/decrypt/sign/verify). Keeps the
        # AES-256-GCM + Ed25519 surface unit-testable in isolation. When
        # the hybrid suite is configured the PQ signer is attached so
        # outbound envelopes carry both signatures.
        pq_signer = PqSigner(own_pq_seed) if own_pq_seed else None
        self._encoder = FederationEncoder(
            own_identity_seed,
            pq_signer=pq_signer,
            sig_suite=sig_suite,
        )
        # §11 QR-code pairing handshake delegate. The peer-pairing
        # client (plaintext Ed25519-signed bootstrap transport) is
        # attached right after construction — circular-free.
        self._pairing = PairingCoordinator(
            federation_repo,
            key_manager,
            own_identity_pk,
            own_pq_pk=own_pq_pk,
            own_sig_suite=sig_suite,
            bus=bus,
        )
        self._pairing.attach_peer_pairing_client(
            PeerPairingClient(
                own_identity_seed=own_identity_seed,
                client_factory=self._get_http_client,
            ),
        )
        # §24.11 inbound validation pipeline (middleware chain).
        self._inbound_pipeline = None  # lazy-built on first use
        self._rtc_inbound_pipeline = None  # lazy-built on first RTC frame
        # Event dispatch registry for federation event handlers.
        # Handlers register themselves via attach_* methods.
        self._event_registry = EventDispatchRegistry()
        self._register_default_handlers()
        # Subscribe to domain events that trigger outbound federation.
        self._bus.subscribe(
            LocalHomeLocationUpdated,
            self._on_local_home_location_updated,
        )

    def _build_inbound_pipeline(self):
        """Lazily construct the §24.11 validation middleware chain.

        Must be called after ``attach_idempotency_cache`` since the
        pipeline references it. Built on first inbound envelope rather
        than in ``__init__`` so all optional wiring is in place.
        """
        return InboundPipeline(
            self._common_pipeline_steps(
                lookup_step=make_lookup_instance(
                    repo=self._federation_repo,
                    lookup_fn=_lookup_by_inbox_id,
                ),
            )
        )

    def _build_rtc_inbound_pipeline(self):
        """Build the §24.11 pipeline variant for WebRTC DataChannel frames.

        Identical to the HTTPS inbox variant except the instance lookup uses
        ``instance_id`` (already known from the peer connection) instead
        of ``inbox_id``.
        """
        return InboundPipeline(
            self._common_pipeline_steps(
                lookup_step=make_lookup_instance_by_id(
                    repo=self._federation_repo,
                ),
            )
        )

    def _common_pipeline_steps(self, *, lookup_step):
        """Return the shared step list for both inbox and RTC pipelines."""
        steps = [
            make_parse_json(loads=_loads),
            lookup_step,
            make_check_timestamp(),
            make_verify_signature(encoder=self._encoder),
            make_check_replay(replay_cache=self._replay_cache),
            make_decrypt_and_parse(
                key_manager=self._key_manager,
                encoder=self._encoder,
                loads=_loads,
            ),
            make_idempotency_check(
                cache_holder=lambda: self._idempotency_cache,
            ),
            make_ban_check(federation_repo=self._federation_repo),
            make_persist_replay(federation_repo=self._federation_repo),
        ]
        # Receiver-side deprovisioned-author backstop. Runs LAST so the
        # replay-id is recorded regardless — if we dropped this here on
        # a later retry the sender's outbox would still get a 200 OK
        # (early-response) and stop redelivering. ``user_repo`` is
        # threaded in via :meth:`attach_user_repo`; if it's unset we
        # skip the step so legacy fixtures still build a working
        # pipeline.
        if self._user_repo is not None:
            steps.append(
                make_check_deprovisioned_author(user_repo=self._user_repo),
            )
        return steps

    # ─── Wiring helpers ──────────────────────────────────────────────────

    def attach_sync_manager(self, sync_manager) -> None:
        """Attach a :class:`SyncSessionManager` after construction.

        Also wires the manager's outbound-ICE emitter so each
        ``SyncRtcSession``'s locally-gathered ICE candidates are
        trickled to the peer via :class:`FederationEventType.SPACE_SYNC_ICE`.
        Without this hook, sync RTC handshakes could only succeed when
        the host candidates inlined in the SDP were reachable
        (same-LAN); cross-NAT sessions hit the 15 s connectivity
        timeout.
        """
        self._sync_manager = sync_manager
        sync_manager._on_local_ice = self._send_outbound_sync_ice

    async def _send_outbound_sync_ice(
        self,
        sync_id: str,
        candidate: str,
        sdp_mid: str,
    ) -> None:
        """Forward a locally-gathered ICE candidate to the peer.

        Looks up the session by ``sync_id`` to figure out who the
        peer is (provider sends to requester, requester to provider).
        Skips when the session has gone away (close raced the
        callback). All inputs are already validated by libdatachannel
        before the candidate enters the iterator.
        """
        if self._sync_manager is None:
            return
        record = self._sync_manager.get_session(sync_id)
        if record is None:
            return
        # Provider sends to the requester; requester sends to the
        # provider. ``provider_instance_id`` is empty on a
        # requester-side record that was opened by ``apply_offer``
        # before a peer was known; in that case the offer's
        # ``from_instance`` was the provider — but the only way the
        # session got into _sessions on the requester side is via
        # ``apply_offer`` which doesn't record the provider id. The
        # easier path: drop the outbound when the target is unknown
        # — the inline candidates in the SDP still carry the host
        # set, and the peer will trickle their own to us regardless.
        if record.rtc is None or record.rtc.role is None:
            return
        if record.rtc.role == "provider":
            target = record.requester_instance_id
        else:
            target = record.provider_instance_id
        if not target:
            return
        await self.send_event(
            to_instance_id=target,
            event_type=FederationEventType.SPACE_SYNC_ICE,
            payload={
                "sync_id": sync_id,
                "candidate": candidate,
                "sdp_mid": sdp_mid,
            },
            space_id=record.space_id,
        )

    def attach_user_repo(self, user_repo) -> None:
        """Attach an :class:`AbstractUserRepo` after construction.

        Enables the §24.11 receiver-side deprovisioned-author filter —
        envelopes whose author user is marked ``deprovisioned_at`` in
        ``remote_users`` are dropped before dispatch. The check runs
        last in the pipeline so the replay-id is still persisted (the
        sender's outbox sees the 200 OK and stops redelivering).
        """
        self._user_repo = user_repo

    def attach_idempotency_cache(self, cache) -> None:
        """Attach an :class:`IdempotencyCache` for inbound dedup."""
        self._idempotency_cache = cache

    def attach_typing_service(self, typing_service) -> None:
        """Attach a :class:`TypingService` for DM_USER_TYPING dispatch."""
        self._typing_service = typing_service
        self._event_registry.register(
            FederationEventType.DM_USER_TYPING,
            self._handle_dm_user_typing,
        )

    def attach_dm_routing(self, dm_routing_service) -> None:
        """Attach a :class:`DmRoutingService` for DM_RELAY dispatch."""
        self._dm_routing_service = dm_routing_service
        self._event_registry.register(
            FederationEventType.DM_RELAY,
            self._handle_dm_relay,
        )

    def attach_transport(self, transport) -> None:
        """Attach a :class:`FederationTransport` facade.

        Once attached, :meth:`send_event` prefers its WebRTC
        DataChannel and falls back to HTTPS inbox only if the channel is
        unavailable.
        """
        self._transport = transport
        for event_type in (
            FederationEventType.FEDERATION_RTC_OFFER,
            FederationEventType.FEDERATION_RTC_ANSWER,
            FederationEventType.FEDERATION_RTC_ICE,
        ):
            self._event_registry.register(event_type, self._handle_transport_event)

    def attach_presence_service(self, presence_service) -> None:
        """Attach :class:`PresenceService` so ``PRESENCE_UPDATED`` lands."""
        self._presence_service = presence_service
        self._event_registry.register(
            FederationEventType.PRESENCE_UPDATED,
            self._handle_presence_updated,
        )

    def attach_online_status_service(self, online_status_service) -> None:
        """Attach :class:`OnlineStatusService` so cross-instance USER_ONLINE
        / USER_IDLE / USER_OFFLINE events land in the remote-state cache."""
        self._online_status_service = online_status_service
        for event_type in (
            FederationEventType.USER_ONLINE,
            FederationEventType.USER_IDLE,
            FederationEventType.USER_OFFLINE,
        ):
            self._event_registry.register(
                event_type,
                self._handle_user_online_changed,
            )

    def attach_space_sync(self, *, service, receiver) -> None:
        """Attach :class:`SpaceSyncService` + :class:`SpaceSyncReceiver`
        so direct-peer chunk streaming works when a DataChannel opens."""
        self._space_sync_service = service
        self._space_sync_receiver = receiver

    def attach_mesh(
        self,
        *,
        route_service: RouteDiscoveryService,
        routed_handler: SpaceRoutedHandler,
    ) -> None:
        """Wire the §D2-PR2 federation-mesh routing pair.

        Once attached, :meth:`send_with_mesh_fallback` falls back to
        SPACE_ROUTED multi-hop delivery when a peer is not directly
        CONFIRMED. Without it, the helper surfaces a failed
        :class:`DeliveryResult` for unconfirmed peers so the caller
        can decide whether to fail-hard or skip.
        """
        self._route_service = route_service
        self._routed_handler = routed_handler

    def attach_gfs_connection_service(self, gfs_connection_service) -> None:
        """Attach :class:`GfsConnectionService` so spec §24.10.7 works.

        The provider asks the GFS for a least-loaded ``signaling_node``
        URL before generating ``SPACE_SYNC_OFFER``, and releases the
        slot when the direct path opens or fails. Without this attach,
        offers ship without ``signaling_node`` (single-node behaviour).
        """
        self._gfs_connection_service = gfs_connection_service

    def attach_call_signaling(self, call_signaling) -> None:
        """Attach a :class:`CallSignalingService` after construction."""
        self._call_signaling = call_signaling
        for event_type in (
            FederationEventType.CALL_OFFER,
            FederationEventType.CALL_ANSWER,
            FederationEventType.CALL_DECLINE,
            FederationEventType.CALL_BUSY,
            FederationEventType.CALL_HANGUP,
            FederationEventType.CALL_END,
            FederationEventType.CALL_ICE,
            FederationEventType.CALL_ICE_CANDIDATE,
            FederationEventType.CALL_QUALITY,
        ):
            self._event_registry.register(event_type, self._handle_call_signal)

    def set_ice_servers(self, servers: list[dict]) -> None:
        """Update the WebRTC ICE-server config served to peers.

        Propagates the new list to the attached transport so future
        DataChannel handshakes pick it up. Existing peers keep their
        current config — renegotiation is out of scope.
        """
        self._ice_servers = list(servers or [])
        if self._transport is not None and hasattr(self._transport, "set_ice_servers"):
            self._transport.set_ice_servers(self._ice_servers)

    @property
    def own_instance_id(self) -> str:
        return self._own_instance_id

    async def peer_supports(self, instance_id: str, *, min_version: int) -> bool:
        """``True`` iff the named peer's advertised ``proto_version`` is
        at least ``min_version``.

        Used by outbound senders to gate optional fields on what the
        receiving peer actually understands — see
        :mod:`socialhome.domain.federation_capabilities` for the
        per-version thresholds. An unknown peer (not yet in
        ``remote_instances``) returns ``False``; a peer that hasn't
        sent its ``INSTANCE_CAPABILITIES_UPDATED`` envelope yet reads
        as ``proto_version=1`` (the most conservative wire), so any
        gate above v1 will return ``False`` until the announcement
        arrives. The conservative default is "feature not supported"
        so the sender omits the optional field.

        Fail-soft: if the repo lookup raises we return ``False`` —
        sending the legacy shape is always safer than crashing the
        outbound path mid-fan-out.
        """
        if not instance_id or min_version <= 0:
            return False
        try:
            peer = await self._federation_repo.get_instance(instance_id)
        except Exception:  # pragma: no cover — defensive
            return False
        if peer is None:
            return False
        return peer.proto_version >= min_version

    @property
    def own_identity_seed(self) -> bytes:
        return self._own_identity_seed

    # ─── Lifecycle ────────────────────────────────────────────────────────

    async def warm_replay_cache(self) -> None:
        """Load recent replay-cache entries from the DB into memory.

        Call this once at startup so the in-memory cache is populated before
        any inbound requests are handled.
        """
        entries = await self._federation_repo.load_replay_cache(within_hours=1)
        self._replay_cache.load(entries)

    # ─── HTTP client helper ───────────────────────────────────────────────

    def attach_session(self, session: aiohttp.ClientSession) -> None:
        """Provide the shared aiohttp session after construction.

        Called from ``app._on_startup`` once the app-wide session has
        been created. Tests may inject a session via the ``http_client``
        constructor kwarg instead.
        """
        if self._http_client is None:
            self._http_client = session

    async def _get_http_client(self):
        """Return the wired aiohttp session.

        Retained as a callable so the ``FederationTransport`` inbox
        strategy can defer client resolution to delivery time.
        """
        if self._http_client is None:
            raise RuntimeError(
                "FederationService used before attach_session — "
                "no aiohttp client wired",
            )
        return self._http_client

    # ─── Outbound ─────────────────────────────────────────────────────────

    async def send_event(
        self,
        *,
        to_instance_id: str,
        event_type: FederationEventType,
        payload: dict,
        space_id: str | None = None,
    ) -> DeliveryResult:
        """Encrypt, sign, and deliver a federation event to a peer.

        Steps (§24.11 §1-§8):

        1. Look up ``RemoteInstance`` by ``to_instance_id``.
        2. Decrypt ``key_self_to_remote`` via ``KeyManager``.
        3. Encrypt the payload JSON with AES-256-GCM.
        4. Build ``FederationEnvelope``.
        5. Sign the serialised envelope with ``own_identity_seed``.
        6. POST to ``remote_inbox_url``.
        7. On success: ``mark_reachable``, return ``DeliveryResult(ok=True)``.
        8. On failure: ``mark_unreachable``, enqueue to outbox, return
           ``DeliveryResult(ok=False)``.
        """
        instance = await self._federation_repo.get_instance(to_instance_id)
        if instance is None:
            log.warning("send_event: unknown instance %s", to_instance_id)
            return DeliveryResult(
                instance_id=to_instance_id,
                ok=False,
                error="unknown_instance",
            )

        # Decrypt the directional session key (stored KEK-encrypted).
        try:
            session_key = self._key_manager.decrypt(instance.key_self_to_remote)
        except Exception as exc:
            # §Audit #12: don't surface the underlying crypto exception
            # text at error level — that leaks detail useful to a key-
            # tampering attacker and the operator can see the full
            # traceback at debug. Fixed-string warn is enough.
            log.warning(
                "send_event: failed to decrypt session key for %s",
                to_instance_id,
            )
            log.debug(
                "send_event: key decrypt error detail for %s: %s",
                to_instance_id,
                exc,
            )
            return DeliveryResult(
                instance_id=to_instance_id,
                ok=False,
                error="key_decrypt_error",
            )

        # Encrypt the payload.
        payload_json = _dumps(payload)
        encrypted_payload = self._encrypt_payload(payload_json, session_key)

        # Build the envelope. The per-peer sig_suite (negotiated at
        # pairing time) decides which algorithms sign this envelope.
        msg_id = str(uuid.uuid4())
        timestamp = datetime.now(timezone.utc).isoformat()
        effective_suite = instance.sig_suite or self._encoder.sig_suite
        envelope_dict: dict = {
            "msg_id": msg_id,
            "event_type": event_type.value,
            "from_instance": self._own_instance_id,
            "to_instance": to_instance_id,
            "timestamp": timestamp,
            "encrypted_payload": encrypted_payload,
            "space_id": space_id,
            "proto_version": 1,
            "sig_suite": effective_suite,
        }
        # Signatures cover everything except the ``signatures`` field itself.
        envelope_bytes = _dumps(envelope_dict).encode("utf-8")
        envelope_dict["signatures"] = self._encoder.sign_envelope_all(
            envelope_bytes,
            suite=effective_suite,
        )

        # Dispatch the envelope. When a FederationTransport facade is
        # attached, it decides between WebRTC DataChannel (§24.12.5
        # primary) and HTTPS inbox (fallback). Without the facade we
        # run the legacy inline HTTPS-inbox path — still used by federation-
        # level tests that don't construct the facade.
        # Previous reachability state — used to fire ConnectionReachable
        # on the unreachable → reachable transition only (no noise on every
        # successful send).
        was_unreachable = instance.unreachable_since is not None

        status_code: int | None = None
        if self._transport is not None:
            result = await self._transport.send(
                instance=instance,
                envelope_dict=envelope_dict,
            )
            if result.ok:
                await self._federation_repo.mark_reachable(to_instance_id)
                if was_unreachable:
                    await self._bus.publish(
                        ConnectionReachable(instance_id=to_instance_id),
                    )
                return DeliveryResult(
                    instance_id=to_instance_id,
                    ok=True,
                    status_code=result.status_code,
                )
            status_code = result.status_code
        else:
            try:
                client = await self._get_http_client()
                async with client.post(
                    instance.remote_inbox_url,
                    json=envelope_dict,
                    timeout=_aiohttp_timeout(10),
                ) as resp:
                    status_code = resp.status
                    if 200 <= status_code < 300:
                        await self._federation_repo.mark_reachable(to_instance_id)
                        if was_unreachable:
                            await self._bus.publish(
                                ConnectionReachable(instance_id=to_instance_id),
                            )
                        return DeliveryResult(
                            instance_id=to_instance_id,
                            ok=True,
                            status_code=status_code,
                        )
                    log.warning(
                        "send_event: peer %s returned HTTP %d",
                        to_instance_id,
                        status_code,
                    )
            except Exception as exc:
                log.warning(
                    "send_event: transport error to %s: %s",
                    to_instance_id,
                    exc,
                )
                status_code = None

        # Delivery failed — mark and enqueue for retry.
        await self._federation_repo.mark_unreachable(to_instance_id)
        await self._outbox_repo.enqueue(
            instance_id=to_instance_id,
            event_type=event_type,
            payload_json=_dumps(envelope_dict),
            msg_id=msg_id,
        )
        return DeliveryResult(
            instance_id=to_instance_id,
            ok=False,
            status_code=status_code if isinstance(status_code, int) else None,
            error="delivery_failed",
        )

    async def send_with_mesh_fallback(
        self,
        *,
        to_instance_id: str,
        event_type: FederationEventType,
        payload: dict,
        space_id: str | None = None,
    ) -> DeliveryResult:
        """Deliver to ``to_instance_id`` direct when CONFIRMED, else via mesh.

        Behaviour:

        * Peer is :data:`PairingStatus.CONFIRMED`: delegate to
          :meth:`send_event`. Same shape and side effects as a normal
          outbound.
        * Peer row is missing or non-CONFIRMED AND a mesh pair is attached
          via :meth:`attach_mesh`: discover a path via
          :class:`RouteDiscoveryService` and ship SPACE_ROUTED via
          :class:`SpaceRoutedHandler`. Returns ``DeliveryResult(ok=True)``
          on success; ``error="no_route"`` if discovery found nothing.
        * Peer non-CONFIRMED and mesh not attached: returns
          ``DeliveryResult(ok=False, error="not_confirmed")`` — the caller
          decides whether to surface this as a hard error.

        Does NOT raise on transport failure — callers branch on
        ``result.ok``.
        """
        instance = await self._federation_repo.get_instance(to_instance_id)
        if instance is not None and instance.status is PairingStatus.CONFIRMED:
            return await self.send_event(
                to_instance_id=to_instance_id,
                event_type=event_type,
                payload=payload,
                space_id=space_id,
            )
        if self._route_service is None or self._routed_handler is None:
            return DeliveryResult(
                instance_id=to_instance_id,
                ok=False,
                error="not_confirmed",
            )
        discovery = await self._route_service.discover_route(to_instance_id)
        if discovery is None:
            return DeliveryResult(
                instance_id=to_instance_id,
                ok=False,
                error="no_route",
            )
        path, target_eph_pk = discovery
        if len(path) < 2:
            return DeliveryResult(
                instance_id=to_instance_id,
                ok=False,
                error="no_route",
            )
        try:
            await self._routed_handler.send_routed(
                path=path,
                target_eph_pk_b64=target_eph_pk,
                inner_event_type=event_type,
                inner_payload=payload,
            )
        except Exception as exc:
            log.warning(
                "send_with_mesh_fallback: routed ship to %s failed: %s",
                to_instance_id,
                exc,
            )
            return DeliveryResult(
                instance_id=to_instance_id,
                ok=False,
                error="routed_send_failed",
            )
        return DeliveryResult(instance_id=to_instance_id, ok=True)

    async def broadcast_to_peers(
        self,
        *,
        event_type: FederationEventType,
        payload: dict,
        instance_ids: list[str] | None = None,
        space_id: str | None = None,
    ) -> BroadcastResult:
        """Send to multiple peers (direct-only).

        If ``instance_ids`` is ``None``, sends to all confirmed peers.
        Mesh fallback is deliberately NOT used here — capability /
        directory broadcasts target direct peers by construction.
        Space-content fanout uses :meth:`broadcast_to_space_members`
        which honours mesh routing per-peer.
        """
        if instance_ids is None:
            instances = await self._federation_repo.list_instances(
                status=PairingStatus.CONFIRMED.value,
            )
            instance_ids = [inst.id for inst in instances]

        results: list[DeliveryResult] = []
        for iid in instance_ids:
            result = await self.send_event(
                to_instance_id=iid,
                event_type=event_type,
                payload=payload,
                space_id=space_id,
            )
            results.append(result)

        succeeded = sum(1 for r in results if r.ok)
        return BroadcastResult(
            attempted=len(results),
            succeeded=succeeded,
            failed=len(results) - succeeded,
            results=tuple(results),
        )

    async def broadcast_to_space_members(
        self,
        space_id: str,
        event_type: FederationEventType,
        payload: dict,
    ) -> BroadcastResult:
        """Fan out to every member household of ``space_id``.

        Each per-peer ship goes through :meth:`send_with_mesh_fallback`
        so a member whose household is *not* a direct CONFIRMED peer
        (admin-initiated private invite accepted via mesh, post-pairing
        churn, transient unpair) still receives the envelope via
        SPACE_ROUTED when the mesh is wired. Mesh-only members are
        looked up via :meth:`AbstractFederationRepo.list_member_instance_ids`
        — that path skips the ``remote_instances.status = CONFIRMED``
        filter so an unconfirmed-but-reachable member still appears
        in the broadcast set; the per-peer ``send_with_mesh_fallback``
        decides direct vs mesh based on the actual pairing state at
        send time.
        """
        instance_ids = await self._federation_repo.list_member_instance_ids(
            space_id,
        )
        results: list[DeliveryResult] = []
        for iid in instance_ids:
            result = await self.send_with_mesh_fallback(
                to_instance_id=iid,
                event_type=event_type,
                payload=payload,
                space_id=space_id,
            )
            results.append(result)
        succeeded = sum(1 for r in results if r.ok)
        return BroadcastResult(
            attempted=len(results),
            succeeded=succeeded,
            failed=len(results) - succeeded,
            results=tuple(results),
        )

    # ─── Inbound ──────────────────────────────────────────────────────────

    async def handle_inbound_envelope(
        self,
        inbox_id: str,
        raw_body: bytes,
    ) -> dict:
        """§24.11 validation pipeline for an inbound federation inbox.

        Delegates to :class:`InboundPipeline` — a composable middleware
        chain where each step (JSON parse → instance lookup → timestamp
        check → signature verify → replay check → decrypt → idempotency
        → ban check → persist replay) is an independently-testable
        async callable. See ``federation/inbound_validator.py``.

        Returns ``{"status": "ok"}`` on success.
        Raises ``ValueError`` on any validation failure (caller returns 400/403).
        """
        if self._inbound_pipeline is None:
            self._inbound_pipeline = self._build_inbound_pipeline()

        pipeline: InboundPipeline = self._inbound_pipeline  # type: ignore[assignment]
        ctx = InboundContext(raw_body=raw_body, inbox_id=inbox_id)
        result = await pipeline.run(ctx)

        # Early-response means a step (e.g. idempotency) short-circuited.
        if ctx.early_response is not None:
            return result

        # Dispatch the validated event.
        if ctx.event is not None:
            await self._dispatch_event(ctx.event)

        return result

    async def handle_inbound_rtc(
        self,
        instance_id: str,
        raw_body: bytes,
    ) -> dict:
        """§24.11 validation pipeline for a WebRTC DataChannel frame.

        Same pipeline as :meth:`handle_inbound_envelope` but resolves
        the sender by ``instance_id`` (already known from the peer
        connection) instead of ``inbox_id``.
        """
        if self._rtc_inbound_pipeline is None:
            self._rtc_inbound_pipeline = self._build_rtc_inbound_pipeline()

        pipeline: InboundPipeline = self._rtc_inbound_pipeline  # type: ignore[assignment]
        ctx = InboundContext(raw_body=raw_body, instance_id=instance_id)
        result = await pipeline.run(ctx)

        if ctx.early_response is not None:
            return result

        if ctx.event is not None:
            await self._dispatch_event(ctx.event)

        return result

    async def _dispatch_event(self, event: FederationEvent) -> None:
        """Route a validated inbound event to registered handlers.

        Handlers register themselves via attach_* methods on the event dispatcher.
        This eliminates if/elif chains and None checks — each handler is only
        invoked if it was registered.
        """
        log.debug(
            "federation event from=%s type=%s space=%s",
            event.from_instance,
            event.event_type,
            event.space_id,
        )

        await self._event_registry.dispatch(event)

    def _register_default_handlers(self) -> None:
        """Register built-in event handlers available in all configurations.

        Optional services register additional handlers via attach_* methods.
        """
        # Space config — always active
        self._event_registry.register(
            FederationEventType.SPACE_CONFIG_CHANGED,
            self._handle_space_config_changed,
        )

        # Pairing intro relay — always active
        self._event_registry.register(
            FederationEventType.PAIRING_INTRO_RELAY,
            self._handle_pairing_intro_relay,
        )

        # Direct DataChannel sync handlers — always active
        self._event_registry.register(
            FederationEventType.SPACE_SYNC_BEGIN,
            self._handle_space_sync_begin,
        )
        self._event_registry.register(
            FederationEventType.SPACE_SYNC_OFFER,
            self._handle_space_sync_offer,
        )
        self._event_registry.register(
            FederationEventType.SPACE_SYNC_ANSWER,
            self._handle_space_sync_answer,
        )
        self._event_registry.register(
            FederationEventType.SPACE_SYNC_ICE,
            self._handle_space_sync_ice,
        )
        self._event_registry.register(
            FederationEventType.SPACE_SYNC_DIRECT_READY,
            self._handle_space_sync_direct_ready,
        )
        self._event_registry.register(
            FederationEventType.SPACE_SYNC_DIRECT_FAILED,
            self._handle_space_sync_direct_failed,
        )
        self._event_registry.register(
            FederationEventType.SPACE_SYNC_REQUEST_MORE,
            self._handle_space_sync_request_more,
        )
        self._event_registry.register(
            FederationEventType.SPACE_SYNC_COMPLETE,
            self._handle_space_sync_complete,
        )
        self._event_registry.register(
            FederationEventType.INSTANCE_SYNC_STATUS,
            self._handle_instance_sync_status,
        )

        # Peer home-location update — always active
        self._event_registry.register(
            FederationEventType.LOCAL_HOME_LOCATION_CHANGED,
            self._on_local_home_location_changed,
        )

        # Inbound media validation — strip non-conforming file_meta from
        # post payloads so the text is kept but invalid media is dropped.
        for _evt in (
            FederationEventType.SPACE_POST_CREATED,
            FederationEventType.SPACE_POST_UPDATED,
        ):
            self._event_registry.register(_evt, self._validate_inbound_media)

    # ─── Event handler dispatch (registry pattern) ───────────────────────────

    async def _handle_space_config_changed(self, event: FederationEvent) -> None:
        if event.space_id:
            await self._bus.publish(
                SpaceConfigChanged(
                    space_id=event.space_id,
                    event_type=event.event_type.value,
                    payload=event.payload,
                    sequence=int(event.payload.get("sequence", 0)),
                )
            )

    async def _validate_inbound_media(self, event: FederationEvent) -> None:
        """Validate ``file_meta`` in SPACE_POST_CREATED / SPACE_POST_UPDATED.

        On failure the ``file_meta`` key is stripped from the payload so
        downstream handlers still receive the post text. A warning is logged
        so operators can spot non-conforming peers.
        """
        file_meta = event.payload.get("file_meta")
        if file_meta is None:
            return
        try:
            validate_inbound_media_meta(file_meta)
        except ValueError as exc:
            log.warning(
                "Stripping invalid file_meta from %s (from=%s): %s",
                event.event_type,
                event.from_instance,
                exc,
            )
            event.payload.pop("file_meta", None)

    async def _on_local_home_location_changed(self, event: FederationEvent) -> None:
        """Inbound LOCAL_HOME_LOCATION_CHANGED: update peer row + publish PeerHomeChanged."""
        if "latitude" not in event.payload or "longitude" not in event.payload:
            log.warning(
                "LOCAL_HOME_LOCATION_CHANGED from %s missing latitude/longitude",
                event.from_instance,
            )
            return
        lat_raw = event.payload["latitude"]
        lon_raw = event.payload["longitude"]
        # Both-null = revoke signal: peer asks us to clear our copy.
        if lat_raw is None and lon_raw is None:
            latitude: float | None = None
            longitude: float | None = None
        elif lat_raw is None or lon_raw is None:
            log.warning(
                "LOCAL_HOME_LOCATION_CHANGED from %s has asymmetric nulls; ignored",
                event.from_instance,
            )
            return
        else:
            try:
                latitude = round(float(lat_raw), 4)
                longitude = round(float(lon_raw), 4)
            except (TypeError, ValueError):  # fmt: skip
                log.warning(
                    "LOCAL_HOME_LOCATION_CHANGED from %s has non-numeric coordinates",
                    event.from_instance,
                )
                return
        await self._federation_repo.update_instance_home(
            event.from_instance,
            latitude=latitude,
            longitude=longitude,
        )
        await self._bus.publish(
            PeerHomeChanged(
                instance_id=event.from_instance,
                latitude=latitude,
                longitude=longitude,
            ),
        )

    # ─── Domain-event subscribers (outbound fan-out) ─────────────────────────

    async def _on_local_home_location_updated(
        self,
        event: LocalHomeLocationUpdated,
    ) -> None:
        """Fan out LOCAL_HOME_LOCATION_CHANGED to every confirmed peer at proto_version >= 5 that has share_home enabled."""
        peers = await self._federation_repo.list_instances(status="confirmed")
        payload = {"latitude": event.latitude, "longitude": event.longitude}
        for peer in peers:
            if not peer.share_home:
                continue
            if not await self.peer_supports(
                peer.id,
                min_version=FederationCapability.MIN_FOR_HOME_LOCATION_BROADCAST,
            ):
                continue
            # Serial fanout: send_event does inline HTTP I/O; gather() would
            # parallelise at the cost of more complex error handling — acceptable
            # for the typical 1-3 peer count of a home instance.
            await self.send_event(
                to_instance_id=peer.id,
                event_type=FederationEventType.LOCAL_HOME_LOCATION_CHANGED,
                payload=payload,
            )

    async def _handle_pairing_intro_relay(self, event: FederationEvent) -> None:
        """§11.9 friend-of-friend introduction request."""
        target = str(event.payload.get("target_instance_id") or "")
        message = str(event.payload.get("message") or "")[:500]
        log.info(
            "PAIRING_INTRO_RELAY: %s wants to introduce %s (via us)",
            event.from_instance,
            target,
        )
        await self._bus.publish(
            PairingIntroRelayReceived(
                from_instance=event.from_instance,
                target_instance_id=target,
                message=message,
            )
        )

    async def _handle_dm_relay(self, event: FederationEvent) -> None:
        if self._dm_routing_service is not None:
            outcome = await self._dm_routing_service.handle_inbound_relay(event)
            log.debug(
                "DM_RELAY %s from %s → %s",
                event.payload.get("message_id"),
                event.from_instance,
                outcome,
            )

    async def _handle_dm_user_typing(self, event: FederationEvent) -> None:
        if self._typing_service is not None:
            await self._typing_service.handle_remote_typing(event)

    async def _handle_presence_updated(self, event: FederationEvent) -> None:
        if self._presence_service is not None:
            await self._presence_service.apply_remote(
                from_instance=event.from_instance,
                payload=event.payload,
            )
        else:
            log.debug(
                "PRESENCE_UPDATED from %s dropped — no service attached",
                event.from_instance,
            )

    async def _handle_user_online_changed(self, event: FederationEvent) -> None:
        if self._online_status_service is None:
            log.debug(
                "%s from %s dropped — no online-status service attached",
                event.event_type.value,
                event.from_instance,
            )
            return
        await self._online_status_service.apply_remote(
            from_instance=event.from_instance,
            event_type=event.event_type,
            payload=event.payload,
        )

    async def _handle_transport_event(self, event: FederationEvent) -> None:
        """Dispatch P2P federation RTC events to the transport."""
        if self._transport is None:
            return
        match event.event_type:
            case FederationEventType.FEDERATION_RTC_OFFER:
                await self._transport.on_rtc_offer(
                    from_instance=event.from_instance,
                    payload=event.payload,
                )
            case FederationEventType.FEDERATION_RTC_ANSWER:
                await self._transport.on_rtc_answer(
                    from_instance=event.from_instance,
                    payload=event.payload,
                )
            case FederationEventType.FEDERATION_RTC_ICE:
                await self._transport.on_rtc_ice(
                    from_instance=event.from_instance,
                    payload=event.payload,
                )

    async def _handle_call_signal(self, event: FederationEvent) -> None:
        if self._call_signaling is not None:
            await self._call_signaling.handle_federated_signal(event)

    async def _handle_space_sync_complete(self, event: FederationEvent) -> None:
        if self._sync_manager is not None:
            self._sync_manager.close_session(
                event.payload.get("sync_id", ""),
            )

    async def _handle_space_sync_begin(self, event) -> None:
        """Provider receives SPACE_SYNC_BEGIN — admit + create session.

        S-6 / S-8 admission is delegated to :class:`SyncSessionManager`.
        On rejection we send the canonical
        ``SPACE_SYNC_DIRECT_FAILED`` reply via the relay so the
        requester can fall back.
        """
        if self._sync_manager is None:
            return
        payload = event.payload
        sync_id = payload.get("sync_id") or ""
        space_id = event.space_id or payload.get("space_id") or ""
        if not sync_id or not space_id:
            return
        decision = await self._sync_manager.begin_session(
            sync_id=sync_id,
            space_id=space_id,
            requester_instance_id=event.from_instance,
            provider_instance_id=self._own_instance_id,
            sync_mode=str(payload.get("sync_mode", "initial")),
            ice_servers=self._ice_servers,
        )
        if not decision.accepted and decision.next_event is not None:
            await self.send_event(
                to_instance_id=event.from_instance,
                event_type=decision.next_event,
                payload=decision.next_payload or {},
                space_id=space_id,
            )
            return

        if decision.accepted and bool(payload.get("prefer_direct")):
            # Build SDP offer, send SPACE_SYNC_OFFER back over relay.
            record = self._sync_manager.get_session(sync_id)
            if record is not None and record.rtc is not None:
                sdp_offer = await record.rtc.create_offer()
                # Spec §24.10.7 — ask the paired GFS for a signaling
                # node so ICE candidates spread across cluster peers.
                # ``None`` = single-node GFS or no GFS paired; field is
                # then omitted from the offer per the spec.
                signaling_node: str | None = None
                if self._gfs_connection_service is not None:
                    signaling_node = (
                        await self._gfs_connection_service.request_signaling_node(
                            sync_id,
                            from_instance=self._own_instance_id,
                            signing_key=self._own_identity_seed,
                        )
                    )
                    if signaling_node:
                        record.signaling_node = signaling_node
                offer_payload: dict = {
                    "sync_id": sync_id,
                    "sdp_offer": sdp_offer,
                    "ice_servers": self._ice_servers,
                }
                if signaling_node:
                    offer_payload["signaling_node"] = signaling_node
                await self.send_event(
                    to_instance_id=event.from_instance,
                    event_type=FederationEventType.SPACE_SYNC_OFFER,
                    payload=offer_payload,
                    space_id=space_id,
                )

    async def _handle_space_sync_offer(self, event) -> None:
        """Requester receives SPACE_SYNC_OFFER — generate + send answer."""
        if self._sync_manager is None:
            return
        payload = event.payload
        sync_id = payload.get("sync_id") or ""
        sdp_offer = payload.get("sdp_offer") or ""
        space_id = event.space_id or ""
        if not sync_id or not sdp_offer:
            return
        sdp_answer = await self._sync_manager.apply_offer(
            sync_id=sync_id,
            sdp_offer=sdp_offer,
            requester_instance_id=self._own_instance_id,
            space_id=space_id,
            ice_servers=payload.get("ice_servers"),
        )
        await self.send_event(
            to_instance_id=event.from_instance,
            event_type=FederationEventType.SPACE_SYNC_ANSWER,
            payload={"sync_id": sync_id, "sdp_answer": sdp_answer},
            space_id=space_id,
        )

    async def _handle_space_sync_answer(self, event) -> None:
        """Provider receives SPACE_SYNC_ANSWER — applies S-14 origin guard."""
        if self._sync_manager is None:
            return
        payload = event.payload
        sync_id = payload.get("sync_id") or ""
        sdp_answer = payload.get("sdp_answer") or ""
        if not sync_id or not sdp_answer:
            return
        await self._sync_manager.apply_answer(
            sync_id=sync_id,
            sdp_answer=sdp_answer,
            from_instance=event.from_instance,
        )

    async def _handle_space_sync_ice(self, event) -> None:
        """Either side: trickle an ICE candidate through with S-7 validation."""
        if self._sync_manager is None:
            return
        payload = event.payload
        sync_id = payload.get("sync_id") or ""
        candidate = payload.get("candidate") or ""
        if not sync_id or not candidate:
            return
        # ``sdp_mid`` is optional — peers prior to the outbound-trickle
        # work omitted it and the manager defaulted to "0". Forward
        # whatever the sender chose; the manager / RTC session
        # consume the same default if missing.
        sdp_mid = str(payload.get("sdp_mid") or "0")
        await self._sync_manager.apply_ice(
            sync_id=sync_id,
            candidate=candidate,
            sdp_mid=sdp_mid,
        )

    async def _handle_space_sync_direct_ready(self, event) -> None:
        """DataChannel open → provider starts streaming content (§25.6)."""
        log.debug(
            "SPACE_SYNC_DIRECT_READY from %s sync_id=%s",
            event.from_instance,
            event.payload.get("sync_id"),
        )
        if self._sync_manager is None or self._space_sync_service is None:
            return
        sync_id = str(event.payload.get("sync_id") or "")
        if not sync_id:
            return
        session = self._sync_manager.get_session(sync_id)
        if session is None:
            return
        # Only the provider streams — the peer sending READY must be the
        # requester recorded at begin_session time.
        if session.requester_instance_id != event.from_instance:
            return
        await self._release_signaling_node(session)
        asyncio.create_task(
            self._space_sync_service.stream_initial(session),
            name=f"space-sync-initial-{sync_id}",
        )

    async def _handle_space_sync_direct_failed(self, event) -> None:
        """Direct path failed — fall back to relay sync (S-15)."""
        if self._sync_manager is None:
            return
        sync_id = event.payload.get("sync_id") or ""
        if not sync_id:
            return
        session = self._sync_manager.get_session(sync_id)
        if session is not None:
            await self._release_signaling_node(session)
        decision = await self._sync_manager.trigger_relay_sync(sync_id)
        if decision.next_event is not None:
            await self.send_event(
                to_instance_id=event.from_instance,
                event_type=decision.next_event,
                payload=decision.next_payload or {},
                space_id=event.space_id,
            )

    async def _release_signaling_node(self, session) -> None:
        """Release the GFS-side counter for a sync session (spec §24.10.7).

        Idempotent: clears ``session.signaling_node`` after release so a
        subsequent ``DIRECT_FAILED`` after a ``DIRECT_READY`` does not
        decrement twice on this end (the GFS endpoint floors at 0
        anyway).
        """
        if self._gfs_connection_service is None:
            return
        node = session.signaling_node
        if not node:
            return
        session.signaling_node = None
        await self._gfs_connection_service.release_signaling_node(
            session.sync_id,
            node,
            from_instance=self._own_instance_id,
            signing_key=self._own_identity_seed,
        )

    async def _handle_space_sync_request_more(self, event) -> None:
        """Requester asks for an older slice (S-12 bounds check)."""
        if self._sync_manager is None:
            return
        cleaned = await self._sync_manager.clamp_request_more(event.payload)
        if cleaned is None:
            return
        log.debug(
            "SPACE_SYNC_REQUEST_MORE from %s: %s",
            event.from_instance,
            cleaned,
        )
        if self._space_sync_service is None:
            return
        sync_id = str(cleaned.get("sync_id") or event.payload.get("sync_id") or "")
        if not sync_id:
            return
        session = self._sync_manager.get_session(sync_id)
        if session is None:
            return
        asyncio.create_task(
            self._space_sync_service.stream_request_more(session, cleaned),
            name=f"space-sync-more-{sync_id}",
        )

    async def _handle_instance_sync_status(self, event) -> None:
        """Peer reports its known spaces — S-17 origin + cap guard."""
        if self._sync_manager is None:
            return
        spaces = await self._sync_manager.validate_instance_sync_status(
            from_instance=event.from_instance,
            payload=event.payload,
        )
        log.debug(
            "INSTANCE_SYNC_STATUS from %s: accepted %d spaces",
            event.from_instance,
            len(spaces),
        )

    # ─── Pairing ──────────────────────────────────────────────────────────
    # The §11 QR-code pairing flow is implemented in
    # :class:`PairingCoordinator`. The three methods below are thin
    # delegations so the public surface of FederationService is unchanged.

    async def initiate_pairing(self, inbox_base_url: str) -> dict:
        """Delegates to :class:`PairingCoordinator`.

        ``inbox_base_url`` is the external scheme+host+path prefix peers
        will POST to. The coordinator appends a freshly-minted
        ``own_local_inbox_id`` before baking the URL into the QR.
        """
        return await self._pairing.initiate(inbox_base_url)

    async def accept_pairing(
        self,
        qr_payload: dict,
        own_inbox_base_url: str | None = None,
    ) -> dict:
        """Delegates to :class:`PairingCoordinator`."""
        return await self._pairing.accept(qr_payload, own_inbox_base_url)

    async def confirm_pairing(
        self,
        token: str,
        verification_code: str,
    ) -> RemoteInstance:
        """Delegates to :class:`PairingCoordinator`."""
        return await self._pairing.confirm(token, verification_code)

    async def handle_peer_accept(
        self,
        body: dict,
        *,
        expected_local_inbox_id: str | None = None,
    ) -> dict:
        """Delegates to :class:`PairingCoordinator.handle_peer_accept`."""
        return await self._pairing.handle_peer_accept(
            body,
            expected_local_inbox_id=expected_local_inbox_id,
        )

    async def handle_peer_confirm(
        self,
        body: dict,
        *,
        expected_local_inbox_id: str | None = None,
    ) -> dict:
        """Delegates to :class:`PairingCoordinator.handle_peer_confirm`."""
        return await self._pairing.handle_peer_confirm(
            body,
            expected_local_inbox_id=expected_local_inbox_id,
        )

    # ─── Encryption helpers ───────────────────────────────────────────────

    def _encrypt_payload(self, payload_json: str, session_key: bytes) -> str:
        """Delegates to :class:`FederationEncoder`."""
        return self._encoder.encrypt_payload(payload_json, session_key)

    def _decrypt_payload(self, encrypted: str, session_key: bytes) -> str:
        """Delegates to :class:`FederationEncoder`."""
        return self._encoder.decrypt_payload(encrypted, session_key)

    def _sign_envelope(self, envelope_bytes: bytes) -> str:
        """Delegates to :class:`FederationEncoder`."""
        return self._encoder.sign_envelope(envelope_bytes)

    def _verify_signature(
        self,
        envelope_bytes: bytes,
        signature: str,
        public_key: bytes,
    ) -> bool:
        """Delegates to :class:`FederationEncoder`."""
        return self._encoder.verify_signature(
            envelope_bytes,
            signature,
            public_key,
        )


# ─── Internal helpers ─────────────────────────────────────────────────────


def _require_fields(data: dict, *fields: str) -> None:
    """Raise ``ValueError`` if any of ``fields`` are missing from ``data``."""
    missing = [f for f in fields if f not in data]
    if missing:
        raise ValueError(f"Missing required fields: {missing}")


async def _lookup_by_inbox_id(
    repo: AbstractFederationRepo,
    inbox_id: str,
) -> "_InboxInstance | None":
    """Find a ``RemoteInstance`` by its ``local_inbox_id``."""
    inst = await repo.get_instance_by_local_inbox_id(inbox_id)
    return _InboxInstance(inst) if inst is not None else None


def _aiohttp_timeout(seconds: float):
    """Return an ``aiohttp.ClientTimeout``."""
    return aiohttp.ClientTimeout(total=seconds)
