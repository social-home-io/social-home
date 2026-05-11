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
"""

from __future__ import annotations

import logging
from pathlib import Path

from aiohttp import web

from .base import BaseView

log = logging.getLogger(__name__)

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

    The SPA's own router takes over once the document loads.
    """

    _filename = "index.html"


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

    log.info("SPA bundle mounted from %s", static_dir)
    return True
