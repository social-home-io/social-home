"""Route views for the GFS aiohttp application.

Each module groups views by URL concern:

* :mod:`.relay`   — ``/gfs/*`` HTTPS relay endpoints (register, plus
  HTTPS-fallback variants of publish / subscribe / report / appeal +
  spaces / healthz).
* :mod:`.ws`      — ``/gfs/ws`` SH↔GFS WebSocket transport (spec §24.12,
  primary path).
* :mod:`.cluster` — ``/cluster/*`` + admin cluster tab.
* :mod:`.rtc`     — ``/gfs/rtc/*`` SH↔SH WebRTC signalling rendezvous
  for §4.2.3 direct sync (the GFS holds no PeerConnection here).
* :mod:`.admin`   — ``/admin/api/*`` admin portal JSON routes + static
  index + ``/admin/login`` / ``/admin/logout``.

:func:`register_routes` is the single entry point the GFS app uses to
wire every view onto an :class:`aiohttp.web.Application`.
"""

from __future__ import annotations

from pathlib import Path

from aiohttp import web

from .. import admin as admin_mod
from ..public import handle_invite_page, handle_landing, handle_space_page
from .admin import (
    AdminAppealCollectionView,
    AdminAppealDecideView,
    AdminAuditView,
    AdminBrandingHeaderImageView,
    AdminBrandingView,
    AdminClientActionView,
    AdminClientCollectionView,
    AdminClusterCollectionView,
    AdminClusterPeerCollectionView,
    AdminClusterPeerDetailView,
    AdminClusterPeerPingView,
    AdminOverviewView,
    AdminPolicyView,
    AdminReportCollectionView,
    AdminReportReviewView,
    AdminSpaceActionView,
    AdminSpaceCollectionView,
    AdminUiIndexView,
)
from .cluster import (
    ClusterHealthView,
    ClusterSignalingBeginView,
    ClusterSignalingEndView,
    ClusterSyncView,
)
from .relay import (
    AppealView,
    HealthzView,
    PublishView,
    RegisterView,
    ReportView,
    SpacesListView,
    SubscribeView,
)
from .rtc import (
    RtcAnswerView,
    RtcIceView,
    RtcOfferView,
    RtcPingView,
    RtcSessionView,
)
from .stories import (
    StoryOgImageView,
    StoryOgUploadView,
    StoryPublicLandingView,
    StoryPublishView,
    StoryTokenMintView,
    StoryTokenRevokeView,
    StoryUnpublishView,
)
from .story_rtc import (
    StoryIceServersView,
    StoryRtcAnswerView,
    StoryRtcAuthorIceView,
    StoryRtcOfferView,
    StoryRtcSessionView,
    StoryRtcViewerIceView,
)
from .ws import GfsWebSocketView


def register_routes(
    app: web.Application,
    *,
    admin_ui_dir: Path,
    media_dir: str,
    public_static_dir: Path | None = None,
) -> None:
    """Mount every GFS view onto *app*.

    Accepts ``admin_ui_dir`` for the admin-static mount, ``media_dir``
    (absolute) for the public ``/media/`` file mount, and the optional
    ``public_static_dir`` that holds the §stories_public viewer
    bundle. Tests pass ``None`` for the static dir when they don't
    care about the public landing JS.
    """
    # Pairing handshake (spec §24.12 — the only HTTPS hop in the
    # primary flow; everything afterwards rides the WebSocket below).
    app.router.add_view("/gfs/register", RegisterView)

    # Persistent SH↔GFS WebSocket — primary transport (spec §24.12).
    app.router.add_view("/gfs/ws", GfsWebSocketView)

    # HTTPS-fallback wire endpoints — used when the WebSocket cannot
    # stay open. The protocol is otherwise identical to the WS frames.
    app.router.add_view("/gfs/publish", PublishView)
    app.router.add_view("/gfs/subscribe", SubscribeView)
    app.router.add_view("/gfs/report", ReportView)
    app.router.add_view("/gfs/appeal", AppealView)
    app.router.add_view("/gfs/spaces", SpacesListView)
    app.router.add_view("/healthz", HealthzView)

    # Public SSR pages (spec §24.7 / §24.8) — staying procedural since
    # they only implement GET and need no auth / body parsing.
    app.router.add_get("/", handle_landing)
    app.router.add_get("/spaces/{slug}", handle_space_page)
    app.router.add_get("/join/{gfs_token}", handle_invite_page)

    # Cluster (spec §24.10).
    app.router.add_view("/cluster/sync", ClusterSyncView)
    app.router.add_view("/cluster/health", ClusterHealthView)
    # Spec §24.10.7 — round-robin sync-signaling node selection.
    app.router.add_view(
        "/cluster/signaling-session",
        ClusterSignalingBeginView,
    )
    app.router.add_view(
        "/cluster/signaling-session/release",
        ClusterSignalingEndView,
    )

    # SH↔SH WebRTC signalling rendezvous for §4.2.3 direct DataChannel
    # sync. The GFS is a public bulletin board for SDP offers / answers /
    # ICE candidates so two NATted households can find each other; the
    # GFS itself holds no PeerConnection.
    app.router.add_view("/gfs/rtc/offer", RtcOfferView)
    app.router.add_view("/gfs/rtc/answer", RtcAnswerView)
    app.router.add_view("/gfs/rtc/ice", RtcIceView)
    app.router.add_view("/gfs/rtc/ping", RtcPingView)
    app.router.add_view("/gfs/rtc/session/{session_id}", RtcSessionView)

    # Public story publications (§stories_public). Signed wire endpoints
    # for author SH instances; the public landing is anonymous.
    app.router.add_view(
        "/gfs/stories/{story_id}/publish",
        StoryPublishView,
    )
    app.router.add_view(
        "/gfs/stories/{story_id}/tokens",
        StoryTokenMintView,
    )
    app.router.add_view(
        "/gfs/story_tokens/{token}/revoke",
        StoryTokenRevokeView,
    )
    app.router.add_view(
        "/gfs/stories/{story_id}/unpublish",
        StoryUnpublishView,
    )
    app.router.add_view(
        "/gfs/stories/{story_id}/og",
        StoryOgUploadView,
    )
    # Public OG image — fetched by anonymous social-card crawlers.
    # Path is stable per (instance, story) so a token rotation
    # doesn't invalidate the cached preview on Twitter / Slack /
    # iMessage. Mounted before the ``{token}`` route so aiohttp's
    # routing table picks the literal ``og.jpg`` first.
    app.router.add_view(
        "/story/{instance_id}/{story_id}/og.jpg",
        StoryOgImageView,
    )
    app.router.add_view(
        "/story/{instance_id}/{story_id}/{token}",
        StoryPublicLandingView,
    )

    # Public-viewer signalling for the public-story flow. The offer
    # endpoint is anonymous (token-gated); the answer endpoints are
    # Ed25519-signed by the author SH like the rest of /gfs/rtc/*.
    app.router.add_view("/gfs/stories/ice-servers", StoryIceServersView)
    app.router.add_view("/gfs/story_rtc/offer", StoryRtcOfferView)
    app.router.add_view(
        "/gfs/story_rtc/session/{session_id}",
        StoryRtcSessionView,
    )
    app.router.add_view("/gfs/story_rtc/ice/viewer", StoryRtcViewerIceView)
    app.router.add_view("/gfs/story_rtc/answer", StoryRtcAnswerView)
    app.router.add_view("/gfs/story_rtc/ice/author", StoryRtcAuthorIceView)

    # Admin portal — login/logout stay as module-level functions in
    # ``global_server.admin`` since they wire cookie lifecycle.
    app.router.add_post("/admin/login", admin_mod.handle_login)
    app.router.add_post("/admin/logout", admin_mod.handle_logout)

    # Admin static UI.
    app.router.add_view("/admin", AdminUiIndexView)
    if admin_ui_dir.is_dir():
        app.router.add_static("/admin/static/", admin_ui_dir)
    if Path(media_dir).is_dir():
        app.router.add_static("/media/", media_dir)
    if public_static_dir is not None and public_static_dir.is_dir():
        # Hosts the vanilla-JS public-viewer bundle for §stories_public.
        # The landing page references ``/static/story_public_viewer.js``.
        app.router.add_static("/static/", str(public_static_dir))

    # Admin JSON API (all behind admin_auth_middleware).
    app.router.add_view("/admin/api/overview", AdminOverviewView)
    app.router.add_view("/admin/api/clients", AdminClientCollectionView)
    app.router.add_view(
        "/admin/api/clients/{instance_id}/{action}",
        AdminClientActionView,
    )
    app.router.add_view("/admin/api/spaces", AdminSpaceCollectionView)
    app.router.add_view(
        "/admin/api/spaces/{space_id}/{action}",
        AdminSpaceActionView,
    )
    app.router.add_view("/admin/api/policy", AdminPolicyView)
    app.router.add_view("/admin/api/branding", AdminBrandingView)
    app.router.add_view(
        "/admin/api/branding/header-image",
        AdminBrandingHeaderImageView,
    )
    app.router.add_view("/admin/api/reports", AdminReportCollectionView)
    app.router.add_view(
        "/admin/api/reports/{report_id}/review",
        AdminReportReviewView,
    )
    app.router.add_view("/admin/api/appeals", AdminAppealCollectionView)
    app.router.add_view(
        "/admin/api/appeals/{appeal_id}/decide",
        AdminAppealDecideView,
    )
    app.router.add_view("/admin/api/audit", AdminAuditView)

    app.router.add_view("/admin/api/cluster", AdminClusterCollectionView)
    app.router.add_view(
        "/admin/api/cluster/peers",
        AdminClusterPeerCollectionView,
    )
    app.router.add_view(
        "/admin/api/cluster/peers/{node_id}",
        AdminClusterPeerDetailView,
    )
    app.router.add_view(
        "/admin/api/cluster/peers/{node_id}/ping",
        AdminClusterPeerPingView,
    )


__all__ = ["register_routes"]
