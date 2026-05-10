"""GFS public website — landing, space pages, invite links (§24.7 / §24.8).

Server-side-rendered HTML. No JS framework, no build step. Three routes:

* ``GET /``          — landing page (hero + pairing QR + spaces list).
* ``GET /spaces/{slug}`` — per-space public page with deep-link CTA.
* ``GET /join/{gfs_token}`` — invite link landing + deep-link CTA.

Shared QR-token service handles the single-use 10-minute pairing token
(spec §24.7.4) with a 1-new-token-per-30-s-per-IP rate limit.
"""

from __future__ import annotations

import base64
import html as _html
import io
import logging
import secrets
import time
from typing import TYPE_CHECKING

import qrcode  # type: ignore[import-untyped]
from aiohttp import web

from . import app_keys as K

if TYPE_CHECKING:
    from .repositories import AbstractGfsAdminRepo


log = logging.getLogger(__name__)


#: Pairing-token TTL (spec §24.7.4).
PAIR_TOKEN_TTL_SECONDS: int = 600
#: Minimum seconds between new-token requests per IP.
PAIR_TOKEN_MIN_INTERVAL: int = 30
#: Public-listing rate limit per IP (spec §24.7.3).
LISTING_MAX_PER_MINUTE: int = 30


# ─── Token service ──────────────────────────────────────────────────────


class PairingTokenService:
    """Issue + consume single-use pairing tokens."""

    __slots__ = ("_admin_repo",)

    def __init__(self, admin_repo: "AbstractGfsAdminRepo") -> None:
        self._admin_repo = admin_repo

    async def generate(self, client_ip: str) -> tuple[str | None, int]:
        """Return ``(token, remaining_wait)`` — if the caller is in the
        rate-limit window ``token`` is ``None`` and ``remaining_wait`` is
        the seconds until a new one can be issued.
        """
        since = int(time.time()) - PAIR_TOKEN_MIN_INTERVAL
        recent = await self._admin_repo.count_pair_tokens(client_ip, since=since)
        if recent > 0:
            return None, PAIR_TOKEN_MIN_INTERVAL
        token = secrets.token_urlsafe(32)
        await self._admin_repo.save_pair_token(token, client_ip)
        return token, 0

    async def consume(self, token: str) -> bool:
        """Single-use + 10-minute TTL. Returns ``True`` iff consumed."""
        return await self._admin_repo.consume_pair_token(token)


# ─── Listing rate-limit middleware ─────────────────────────────────────


def build_listing_rate_limit():
    """Simple in-memory per-IP rate limiter for the public listing.

    Spec §24.7.3: 30 GETs per minute on ``/`` and ``/spaces/{id}``.
    """
    counters: dict[str, list[float]] = {}

    @web.middleware
    async def _rate_limit(request: web.Request, handler):
        # Only gate the public HTML pages.
        path = request.rel_url.path
        if not (path == "/" or path.startswith("/spaces/")):
            return await handler(request)
        now = time.monotonic()
        ip = _client_ip(request)
        hits = [t for t in counters.get(ip, []) if now - t < 60.0]
        if len(hits) >= LISTING_MAX_PER_MINUTE:
            resp = web.json_response(
                {"error": "rate_limited"},
                status=429,
            )
            resp.headers["Retry-After"] = "60"
            return resp
        hits.append(now)
        counters[ip] = hits
        return await handler(request)

    return _rate_limit


# ─── Helpers ────────────────────────────────────────────────────────────


def _client_ip(request: web.Request) -> str:
    fwd = request.headers.get("X-Forwarded-For", "")
    if fwd:
        return fwd.split(",")[0].strip()
    peer = request.transport.get_extra_info("peername") if request.transport else None
    return str(peer[0]) if peer else "unknown"


def _escape(value: object | None) -> str:
    if value is None:
        return ""
    return _html.escape(str(value), quote=True)


def _render_qr_png_data_uri(payload: str) -> str:
    """Return a data: URI with a PNG-encoded QR code of *payload*."""
    img = qrcode.make(payload, box_size=8, border=2)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def _audience_filter(value: str | None) -> tuple[str, int | None]:
    """Map ``?audience=…`` to a SQL-friendly (label, max_min_age)."""
    if value == "family":
        return "Family", 0
    if value == "teen":
        return "Teen", 13
    if value == "adult":
        return "Adult", None  # only min_age >= 18 — handled separately
    return "All", None


def _render_landing(
    *,
    server_name: str,
    landing_markdown: str,
    header_image_url: str,
    token: str,
    pair_qr_data_uri: str,
    spaces: list[dict],
    search: str,
    audience: str,
    base_url: str,
) -> str:
    rows = []
    for sp in spaces:
        accent = _escape(sp.get("accent_color") or "#ce5d3e")
        rows.append(f"""
          <li class="card" style="border-left:6px solid {accent}">
            <a href="/spaces/{_escape(sp["space_id"])}">
              <strong>{_escape(sp.get("name") or "—")}</strong>
            </a>
            <div class="muted">{sp.get("subscriber_count", 0)} members
              · {sp.get("posts_per_week", 0):.1f} posts/week</div>
            <p>{_escape((sp.get("description") or "")[:120])}</p>
          </li>
        """)
    rows_html = "\n".join(rows) or ('<li class="muted">No active spaces yet.</li>')
    header_html = (
        f'<img src="{_escape(header_image_url)}" alt="" class="hero-image" />'
        if header_image_url
        else ""
    )
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <title>{_escape(server_name)}</title>
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <meta http-equiv="refresh" content="600" />
  <style>
    /* GFS public theme — leans into the Social Home palette
     *   (warm cream surface, terracotta primary, washi-tape accents)
     * so the public landing feels like the same family of products as
     * the SH SPA rather than a stark federation back-end. Pure CSS, no
     * JS, no build step — server-rendered. */
    :root {{
      --warm-bg: #faf6f1;
      --paper:   #fffdf9;
      --ink:     #1f1916;
      --ink-soft:#5b4f48;
      --hair:    #ead8c2;
      --primary: #ce5d3e;
      --primary-soft: #f4dbd0;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      font: 15px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI",
            Roboto, sans-serif;
      margin: 0; color: var(--ink); background: var(--warm-bg);
    }}
    .hero-image {{ display: block; width: 100%; max-height: 240px;
                    object-fit: cover; }}
    main {{ max-width: 860px; margin: 0 auto; padding: 24px; }}
    h1 {{
      font-family: "Lora", "Iowan Old Style", "Palatino Linotype", serif;
      font-size: 32px; margin: 16px 0 4px; letter-spacing: -0.01em;
    }}
    h2 {{
      font-family: "Lora", "Iowan Old Style", "Palatino Linotype", serif;
      font-size: 20px; margin: 0 0 12px;
    }}
    .muted {{ color: var(--ink-soft); font-size: 13px; }}
    section {{
      position: relative;
      border: 1px solid var(--hair); border-radius: 12px;
      padding: 22px; margin: 20px 0; background: var(--paper);
      box-shadow: 0 1px 0 var(--hair),
                  0 18px 36px -28px rgba(26, 24, 20, 0.25);
    }}
    /* Tiny washi-tape accent on the section corner — same vocabulary
     * as the SH SPA's pinned-card aesthetic, just more restrained. */
    section::before {{
      content: ""; position: absolute; top: -8px; right: 28px;
      width: 60px; height: 16px; transform: rotate(-3deg);
      background: var(--primary-soft); opacity: 0.85;
      border-radius: 2px;
    }}
    .pair {{ display: flex; gap: 22px; align-items: center; flex-wrap: wrap; }}
    .pair img {{ width: 200px; height: 200px;
                 background: #fff; padding: 6px;
                 border: 1px solid var(--hair); border-radius: 8px; }}
    .pair code {{ background: var(--warm-bg); border: 1px solid var(--hair);
                 padding: 4px 8px; border-radius: 6px;
                 display: inline-block; word-break: break-all;
                 font-size: 13px; }}
    .filters {{ display: flex; gap: 8px; flex-wrap: wrap; margin: 8px 0; }}
    .filters a {{ padding: 4px 12px; border-radius: 999px;
                  border: 1px solid var(--hair); color: var(--ink-soft);
                  text-decoration: none; background: transparent;
                  font-size: 13px; }}
    .filters a:hover {{ color: var(--ink); border-color: var(--primary); }}
    .filters a.active {{ background: var(--primary); color: #fff;
                         border-color: var(--primary); }}
    input[type=search] {{ padding: 8px 12px; border: 1px solid var(--hair);
                          border-radius: 8px; font: inherit; width: 100%;
                          max-width: 360px; background: var(--warm-bg);
                          color: var(--ink); }}
    input[type=search]:focus {{ outline: 2px solid var(--primary);
                                outline-offset: 1px; }}
    ul {{ list-style: none; padding: 0; margin: 0; }}
    .card {{ background: var(--warm-bg); border: 1px solid var(--hair);
             border-radius: 10px;
             padding: 14px 16px; margin-bottom: 10px;
             transition: transform 100ms, box-shadow 100ms; }}
    .card:hover {{ transform: translateY(-1px);
                  box-shadow: 0 14px 28px -22px rgba(26, 24, 20, 0.4); }}
    .card a {{ color: var(--ink); text-decoration: none; }}
    .card a:hover strong {{ color: var(--primary); }}
    a {{ color: var(--primary); }}
    a:hover {{ color: var(--ink); }}
    .footer-brand {{
      text-align: center; padding: 14px 0 32px;
      color: var(--ink-soft); font-size: 12px;
    }}
  </style>
</head>
<body>
  {header_html}
  <main>
    <h1>{_escape(server_name)}</h1>
    <p class="muted">Community relay running the Social Home federation.</p>
    <div>{landing_markdown}</div>

    <section>
      <h2>Connect your Social Home</h2>
      <div class="pair">
        <img src="{pair_qr_data_uri}" alt="Pairing QR" />
        <div>
          <ol>
            <li>Open Social Home</li>
            <li>Spaces → Discover → ⬡ Global</li>
            <li>Scan this code or paste the token below</li>
          </ol>
          <p><code data-pair-token="{_escape(token)}">{_escape(token)}</code></p>
          <p class="muted">Valid for 10 minutes · single-use.</p>
        </div>
      </div>
    </section>

    <section>
      <h2>Spaces</h2>
      <form method="get" action="/">
        <input name="search" type="search"
               placeholder="Search spaces…" value="{_escape(search)}" />
      </form>
      <div class="filters">
        <a href="/" class="{"active" if not audience else ""}">All</a>
        <a href="/?audience=family"
           class="{"active" if audience == "family" else ""}">Family</a>
        <a href="/?audience=teen"
           class="{"active" if audience == "teen" else ""}">Teen (13+)</a>
        <a href="/?audience=adult"
           class="{"active" if audience == "adult" else ""}">Adult (18+)</a>
      </div>
      <ul>
        {rows_html}
      </ul>
    </section>

    <section>
      <h2>What is Social Home?</h2>
      <p>Social Home is a privacy-first, self-hosted social network.
      Global Federation Servers like {_escape(server_name)} stitch
      households together for public spaces. Learn more at
      <a href="https://socialhome.io" rel="nofollow noopener">
      socialhome.io</a>.</p>
    </section>
  </main>
  <p class="footer-brand">
    A Global Federation Server · powered by
    <a href="https://socialhome.io" rel="nofollow noopener">Social Home</a>
  </p>
</body>
</html>
"""


def _render_space_page(
    *,
    space: dict,
    server_name: str,
    base_url: str,
) -> str:
    accent = _escape(space.get("accent_color") or "#ce5d3e")
    deep_link = f"sh://join-space/{base_url}/spaces/{_escape(space['space_id'])}"
    og_image = _escape(space.get("cover_url") or "")
    og_title = _escape(f"{space.get('name') or ''} — {server_name}")
    og_desc = _escape(space.get("description") or "")
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <title>{og_title}</title>
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <meta property="og:title"       content="{og_title}" />
  <meta property="og:description" content="{og_desc}" />
  <meta property="og:image"       content="{og_image}" />
  <meta property="og:url"         content="{_escape(base_url)}/spaces/{_escape(space["space_id"])}" />
  <meta name="twitter:card"       content="summary_large_image" />
  <style>
    /* GFS per-space public page — same warm SH palette as the landing.
     * The CTA picks up the per-space accent so the page feels keyed
     * to that community, not just the server. */
    :root {{
      --warm-bg: #faf6f1;
      --paper:   #fffdf9;
      --ink:     #1f1916;
      --ink-soft:#5b4f48;
      --hair:    #ead8c2;
      --primary: {accent};
    }}
    body {{
      font: 15px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI",
            Roboto, sans-serif;
      margin: 0; color: var(--ink); background: var(--warm-bg);
    }}
    main {{ max-width: 780px; margin: 0 auto; padding: 24px; }}
    .accent-bar {{ height: 6px; background: var(--primary);
                   margin: 12px 0 22px; border-radius: 3px; }}
    .cover {{ width: 100%; max-height: 360px; object-fit: cover; }}
    h1 {{
      font-family: "Lora", "Iowan Old Style", "Palatino Linotype", serif;
      margin-bottom: 4px; font-size: 30px; letter-spacing: -0.01em;
    }}
    h2 {{
      font-family: "Lora", "Iowan Old Style", "Palatino Linotype", serif;
      font-size: 20px; margin: 0 0 8px;
    }}
    .muted {{ color: var(--ink-soft); }}
    .cta {{ background: var(--primary); color: #fff; padding: 11px 22px;
            border-radius: 999px; display: inline-block;
            text-decoration: none; font-weight: 600; font-size: 15px;
            transition: transform 100ms, filter 100ms; }}
    .cta:hover {{ transform: translateY(-1px); filter: brightness(0.95); }}
    a.secondary {{ color: var(--ink-soft); text-decoration: none; }}
    a.secondary:hover {{ color: var(--primary); }}
    section {{
      margin: 22px 0;
      background: var(--paper);
      border: 1px solid var(--hair); border-radius: 12px;
      padding: 20px;
      box-shadow: 0 1px 0 var(--hair),
                  0 18px 36px -28px rgba(26, 24, 20, 0.22);
    }}
    .cta-section {{ background: transparent; border: none;
                    box-shadow: none; padding: 0; }}
    .footer-brand {{
      text-align: center; padding: 14px 0 32px;
      color: var(--ink-soft); font-size: 12px;
    }}
    .footer-brand a {{ color: var(--primary); }}
  </style>
</head>
<body>
  {'<img class="cover" src="' + og_image + '" alt="" />' if og_image else ""}
  <main>
    <div class="accent-bar"></div>
    <a href="/" class="secondary">← {_escape(server_name)}</a>
    <h1>{_escape(space.get("name") or "—")}</h1>
    <p class="muted">{space.get("subscriber_count", 0)} members</p>

    <section class="cta-section">
      <a class="cta" href="{_escape(deep_link)}">Open in Social Home</a>
    </section>

    <section>
      <h2>About</h2>
      <p>{_escape(space.get("description") or "")}</p>
      {space.get("about_markdown") or ""}
    </section>
  </main>
  <p class="footer-brand">
    Hosted on {_escape(server_name)} · powered by
    <a href="https://socialhome.io" rel="nofollow noopener">Social Home</a>
  </p>
</body>
</html>
"""


def _render_invite_page(
    *,
    token: str,
    space: dict | None,
    server_name: str,
    base_url: str,
) -> str:
    if space is None:
        return "<!doctype html><p>This invite has expired or was revoked.</p>"
    accent = _escape(space.get("accent_color") or "#6366f1")
    deep_link = f"sh://gfs-invite/{base_url}/join/{_escape(token)}"
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <title>Join {_escape(space.get("name") or "")} — {_escape(server_name)}</title>
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <style>
    body {{ font: 15px/1.5 system-ui, sans-serif; margin: 0; color: #1f2937;
            background: #fff; }}
    main {{ max-width: 640px; margin: 0 auto; padding: 20px; }}
    .accent-bar {{ height: 6px; background: {accent}; margin: 10px 0 20px; }}
    h1 {{ font-size: 26px; margin-bottom: 4px; }}
    .cta {{ background: #6366f1; color: #fff; padding: 10px 18px;
            border-radius: 8px; display: inline-block;
            text-decoration: none; font-weight: 600; }}
  </style>
</head>
<body>
  <main>
    <div class="accent-bar"></div>
    <h1>You're invited to {_escape(space.get("name") or "")}</h1>
    <p>on {_escape(server_name)}.</p>
    <p><a class="cta" href="{_escape(deep_link)}">Open in Social Home</a></p>
    <p class="muted">Opens the invite in your Social Home app — you join
    from your own household instance.</p>
  </main>
</body>
</html>
"""


# ─── Handlers ────────────────────────────────────────────────────────────


async def handle_landing(request: web.Request) -> web.Response:
    """GET / — public landing page."""
    cfg = request.app[K.gfs_config_key]
    admin_repo = request.app[K.gfs_admin_repo_key]
    fed_repo = request.app[K.gfs_fed_repo_key]
    token_svc: PairingTokenService = request.app["gfs_token_service"]

    # Settings pulled fresh (admin portal may have changed them).
    server_name = (await admin_repo.get_config("server_name")) or cfg.server_name
    landing_markdown = (
        await admin_repo.get_config("landing_markdown")
    ) or cfg.landing_markdown
    header_image_file = (
        await admin_repo.get_config("header_image_file")
    ) or cfg.header_image_file

    token, _wait = await token_svc.generate(_client_ip(request))
    if token is None:
        token = "please-wait"
    qr_payload = (
        f"sh://gfs-pair/{cfg.base_url}?token={token}"
        if cfg.base_url
        else f"gfs:token={token}"
    )
    qr_data = _render_qr_png_data_uri(qr_payload)

    search = (request.query.get("search") or "").strip()
    audience = (request.query.get("audience") or "").strip()
    active_spaces = await fed_repo.list_spaces(status="active")
    items: list[dict] = []
    for sp in active_spaces:
        if search:
            haystack = f"{sp.name} {sp.description or ''}".lower()
            if search.lower() not in haystack:
                continue
        if audience == "family" and sp.min_age != 0:
            continue
        if audience == "teen" and sp.min_age > 13:
            continue
        if audience == "adult" and sp.min_age < 18:
            continue
        items.append(
            {
                "space_id": sp.space_id,
                "name": sp.name,
                "description": sp.description or "",
                "accent_color": sp.accent_color,
                "subscriber_count": sp.subscriber_count,
                "posts_per_week": sp.posts_per_week,
            }
        )

    header_image_url = (
        f"{cfg.base_url}/media/{header_image_file}" if header_image_file else ""
    )
    html = _render_landing(
        server_name=server_name,
        landing_markdown=_escape(landing_markdown),
        header_image_url=header_image_url,
        token=token,
        pair_qr_data_uri=qr_data,
        spaces=items,
        search=search,
        audience=audience,
        base_url=cfg.base_url,
    )
    return web.Response(text=html, content_type="text/html")


async def handle_space_page(request: web.Request) -> web.Response:
    """GET /spaces/{slug} — per-space public page."""
    cfg = request.app[K.gfs_config_key]
    admin_repo = request.app[K.gfs_admin_repo_key]
    fed_repo = request.app[K.gfs_fed_repo_key]

    slug = request.match_info["slug"]
    space = await fed_repo.get_space(slug)
    if space is None or space.status != "active":
        raise web.HTTPNotFound(reason="Space not found or not published")

    server_name = (await admin_repo.get_config("server_name")) or cfg.server_name
    space_dict = {
        "space_id": space.space_id,
        "name": space.name,
        "description": space.description,
        "about_markdown": space.about_markdown,
        "cover_url": space.cover_url,
        "accent_color": space.accent_color,
        "subscriber_count": space.subscriber_count,
    }
    html = _render_space_page(
        space=space_dict,
        server_name=server_name,
        base_url=cfg.base_url,
    )
    return web.Response(text=html, content_type="text/html")


async def handle_invite_page(request: web.Request) -> web.Response:
    """GET /join/{gfs_token} — invite link landing."""
    cfg = request.app[K.gfs_config_key]
    admin_repo = request.app[K.gfs_admin_repo_key]
    fed_repo = request.app[K.gfs_fed_repo_key]

    token = request.match_info["gfs_token"]
    row = await admin_repo._db.fetchone(  # type: ignore[attr-defined]
        "SELECT space_id, expires_at FROM gfs_invite_tokens WHERE gfs_token=?",
        (token,),
    )
    space = None
    if row is not None:
        expires_at = row["expires_at"]
        if expires_at is None or int(expires_at) > int(time.time()):
            space_row = await fed_repo.get_space(row["space_id"])
            if space_row is not None and space_row.status == "active":
                space = {
                    "name": space_row.name,
                    "accent_color": space_row.accent_color,
                    "space_id": space_row.space_id,
                }
    server_name = (await admin_repo.get_config("server_name")) or cfg.server_name
    html = _render_invite_page(
        token=token,
        space=space,
        server_name=server_name,
        base_url=cfg.base_url,
    )
    return web.Response(
        text=html,
        content_type="text/html",
        status=200 if space else 404,
    )
