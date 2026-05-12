"""Serve the Preact SPA bundle from the backend.

The dev workflow runs Vite at ``:5173`` and proxies ``/api`` to the
backend on ``:8099`` — see ``client/vite.config.ts``. In production
the same backend serves the SPA itself: ``client/`` builds into
``socialhome/static/`` and this module wires those files into the
aiohttp router.

What we mount:

* ``GET /``               → ``static/index.html``
* ``GET /manifest.json``  → ``static/manifest.json``
* ``GET /sw.js``          → ``static/sw.js``
* ``GET /assets/{file}``  → ``static/assets/{file}`` (content-hashed)

The SPA's own router (preact-iso) handles every in-app route, so the
backend doesn't need a catchall for ``/feed`` / ``/spaces/abc`` /
``/setup``. Refreshing the browser on those URLs is the SPA author's
responsibility (use hash routing, or sit the app behind a reverse
proxy that rewrites to ``/index.html``).

Ingress support: when the add-on runs behind HA Supervisor's ingress
proxy the URL prefix is dynamic — ``/api/hassio_ingress/<token>/``
in front of every request. Supervisor stamps the prefix into
``X-Ingress-Path``. :class:`SpaIndexView` substitutes that into the
``<base href>`` tag inside ``index.html`` at request time so every
relative URL the SPA constructs (fetch, WebSocket, navigation)
resolves against the ingress-prefixed document URL. When the header
is absent (standalone / HA-Core-direct mode), the base stays ``/``.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from aiohttp import web

from .base import BaseView

log = logging.getLogger(__name__)

# Replaces the ``<base href="...">`` already present in
# ``client/index.html``. The trailing ``/`` is required — relative URLs
# in HTML resolve against ``<base>`` as a directory, not as a file.
_BASE_HREF_RE = re.compile(r'<base href="[^"]*"\s*/?>')

#: Default location of the built SPA. ``client/vite.config.ts`` writes
#: here via ``build.outDir``; the production wheel ships the same tree
#: under ``socialhome/static/``.
DEFAULT_STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

_static_dir_key: web.AppKey[Path] = web.AppKey("spa_static_dir", Path)


class _SpaFileView(BaseView):
    """Common plumbing for the per-file SPA views.

    Subclasses set :attr:`_filename` and (optionally) override the
    cache header / extra headers. The static directory is read off
    the request app at handler time so tests can swap it in via
    :func:`mount_spa` without monkey-patching a module-level constant.
    """

    _filename: str = ""
    _cache_control: str = "no-cache"
    _extra_headers: dict[str, str] = {}  # noqa: RUF012  (override in subclass)

    async def get(self) -> web.StreamResponse:
        static_dir: Path = self.request.app[_static_dir_key]
        target = static_dir / self._filename
        if not target.is_file():
            raise web.HTTPNotFound()
        headers = {"Cache-Control": self._cache_control, **self._extra_headers}
        return web.FileResponse(target, headers=headers)


class SpaIndexView(_SpaFileView):
    """``GET /`` — serves ``static/index.html`` (no auth, no cache).

    Reads the ``X-Ingress-Path`` header (set by HA Supervisor when
    the request is proxied through the ingress integration) and
    rewrites the ``<base href>`` element inside ``index.html`` so
    the SPA's relative URLs (``./api/me``, ``./api/ws``, …) resolve
    against the ingress-prefixed document URL. When the header is
    absent the base stays ``/``.
    """

    _filename = "index.html"

    async def get(self) -> web.StreamResponse:
        static_dir = self.request.app[_static_dir_key]
        target = static_dir / self._filename
        if not target.is_file():
            raise web.HTTPNotFound()
        ingress_path = self.request.headers.get("X-Ingress-Path", "").rstrip("/")
        base_href = f"{ingress_path}/" if ingress_path else "/"
        # ``index.html`` is small (a few KiB) — reading + substituting
        # in-memory per request is cheaper than maintaining two copies
        # on disk or a per-prefix cache that invalidates on every token
        # rotation. ``Cache-Control: no-cache`` was already required
        # (the bundle is content-hashed but the shell isn't).
        html = target.read_text(encoding="utf-8")
        substituted, count = _BASE_HREF_RE.subn(
            f'<base href="{base_href}">',
            html,
            count=1,
        )
        if count == 0:
            # The template is required to ship a ``<base href="/">``
            # placeholder so the substitution is deterministic. If a
            # future build drops it, fall back to serving the file
            # as-is — the SPA will still load, just without the
            # ingress-prefix rewrite.
            log.warning(
                "index.html has no <base href> placeholder; "
                "ingress prefix injection skipped"
            )
            substituted = html
        return web.Response(
            text=substituted,
            content_type="text/html",
            headers={"Cache-Control": self._cache_control},
        )


class SpaManifestView(_SpaFileView):
    """``GET /manifest.json`` — PWA manifest."""

    _filename = "manifest.json"


class SpaServiceWorkerView(_SpaFileView):
    """``GET /sw.js`` — service worker.

    ``Service-Worker-Allowed: /`` widens the worker's scope to the
    whole origin even though the script lives at ``/sw.js`` (the
    default scope would be the script's own directory). ``no-cache``
    keeps stale workers from sticking around after a deploy.
    """

    _filename = "sw.js"
    _extra_headers = {"Service-Worker-Allowed": "/"}  # noqa: RUF012


class SpaCatchallView(SpaIndexView):
    """Serves the SPA shell for any non-``/api/`` GET path.

    Without this, refreshing the browser on a deep URL (``/feed``,
    ``/spaces/abc``, etc.) — including the prefixed-form
    ``/api/hassio_ingress/<token>/feed`` that HA Ingress proxies as
    ``GET /feed`` on the add-on side — returns 404. The SPA's own
    ``preact-iso`` router can't claim a path the backend doesn't
    serve, so the standard "single-page-app fallback" pattern is to
    serve the shell for every unmatched path and let the client
    router pick the right view.

    ``/api/`` and friends are protected because the catchall is
    registered **last**, after every concrete route. Anything matched
    by an earlier handler (``SpaIndexView`` at ``/``, ``/manifest.json``,
    ``/sw.js``, ``/assets/{file}``, every ``/api/...``) is served by
    that handler; everything else falls through to here and gets the
    SPA shell. The auth middleware's public-path list mirrors this
    exclusion set (see ``_DEFAULT_PUBLIC_PATH_PATTERNS`` in
    ``socialhome/auth.py``) so the catchall stays unauthenticated.
    """


def mount_spa(app: web.Application, static_dir: Path | None = None) -> bool:
    """Wire SPA routes onto ``app``.

    Returns ``True`` when the mount happened, ``False`` when the
    static directory is missing or empty (e.g. a dev environment
    without a ``pnpm --dir client run build``). In the missing-build
    case we log a warning and leave the router untouched so the
    backend still serves ``/api/*`` and ``/healthz`` for the Vite
    dev-server flow.

    ``static_dir`` defaults to :data:`DEFAULT_STATIC_DIR` resolved at
    call time (not import time), so tests can monkeypatch the module
    constant before ``create_app`` runs.
    """
    if static_dir is None:
        static_dir = DEFAULT_STATIC_DIR
    index = static_dir / "index.html"
    if not index.is_file():
        log.warning(
            "SPA bundle missing at %s — backend will only serve /api/*; "
            "run `pnpm --dir client run build` to enable.",
            index,
        )
        return False

    app[_static_dir_key] = static_dir

    assets_dir = static_dir / "assets"
    if assets_dir.is_dir():
        # ``append_version=False`` — bundle filenames are content-hashed
        # by Vite, so aiohttp's auto-versioning query string is noise.
        app.router.add_static("/assets/", str(assets_dir), append_version=False)

    app.router.add_view("/manifest.json", SpaManifestView)
    app.router.add_view("/sw.js", SpaServiceWorkerView)
    app.router.add_view("/", SpaIndexView)
    # Registered LAST so every more-specific route (``/api/...``,
    # ``/healthz``, ``/manifest.json``, ``/sw.js``, ``/assets/...``,
    # ``/``) wins over the catchall.
    app.router.add_view("/{tail:.+}", SpaCatchallView)

    log.info("SPA bundle mounted from %s", static_dir)
    return True
