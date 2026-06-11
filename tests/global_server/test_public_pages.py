"""Tests for the public SSR pages (§24.7 / §24.8)."""

from __future__ import annotations

import pytest
from aiohttp.test_utils import TestClient, TestServer

from socialhome.global_server.app_keys import (
    gfs_admin_repo_key,
    gfs_fed_repo_key,
)
from socialhome.global_server.config import GfsConfig
from socialhome.global_server.domain import ClientInstance, GlobalSpace
from socialhome.global_server.server import create_gfs_app


def _config(tmp_dir):
    return GfsConfig(
        host="127.0.0.1",
        port=0,
        base_url="http://gfs.test",
        data_dir=str(tmp_dir),
        instance_id="gfs-test",
    )


@pytest.fixture
async def client(tmp_dir):
    app = create_gfs_app(_config(tmp_dir))
    async with TestClient(TestServer(app)) as tc:
        tc._app = app
        yield tc


# ─── Landing page ────────────────────────────────────────────────────


async def test_landing_renders_server_name(client):
    resp = await client.get("/")
    assert resp.status == 200
    text = await resp.text()
    assert "My Global Server" in text
    # QR img tag in Connect section.
    assert 'src="data:image/png;base64,' in text


async def test_landing_renders_updated_server_name_from_db(client):
    app = client._app
    await app[gfs_admin_repo_key].set_config(
        "server_name",
        "Pascal's GFS",
    )
    resp = await client.get("/")
    text = await resp.text()
    assert "Pascal&#x27;s GFS" in text or "Pascal's GFS" in text


async def test_landing_lists_active_spaces_only(client):
    app = client._app
    fed_repo = app[gfs_fed_repo_key]
    await fed_repo.upsert_instance(
        ClientInstance(
            instance_id="o.home",
            display_name="O",
            public_key="aa" * 32,
            inbox_url="http://o/wh",
            status="active",
        )
    )
    await fed_repo.upsert_space(
        GlobalSpace(
            space_id="sp-active",
            owning_instance="o.home",
            name="Active Space",
            description="hello",
            accent_color="#ff0000",
            status="active",
        )
    )
    await fed_repo.upsert_space(
        GlobalSpace(
            space_id="sp-pending",
            owning_instance="o.home",
            name="Pending Space",
            status="pending",
        )
    )
    resp = await client.get("/")
    text = await resp.text()
    assert "Active Space" in text
    assert "Pending Space" not in text


async def test_landing_search_filters(client):
    app = client._app
    fed_repo = app[gfs_fed_repo_key]
    await fed_repo.upsert_instance(
        ClientInstance(
            instance_id="o.home",
            display_name="O",
            public_key="aa" * 32,
            inbox_url="http://o/wh",
            status="active",
        )
    )
    await fed_repo.upsert_space(
        GlobalSpace(
            space_id="sp-makers",
            owning_instance="o.home",
            name="Makers Space",
            status="active",
        )
    )
    await fed_repo.upsert_space(
        GlobalSpace(
            space_id="sp-garden",
            owning_instance="o.home",
            name="Garden Club",
            status="active",
        )
    )
    resp = await client.get("/?search=makers")
    text = await resp.text()
    assert "Makers Space" in text
    assert "Garden Club" not in text


async def test_landing_category_filter(client):
    app = client._app
    fed_repo = app[gfs_fed_repo_key]
    await fed_repo.upsert_instance(
        ClientInstance(
            instance_id="o.home",
            display_name="O",
            public_key="aa" * 32,
            inbox_url="http://o/wh",
            status="active",
        )
    )
    await fed_repo.upsert_space(
        GlobalSpace(
            space_id="gaming-sp",
            owning_instance="o.home",
            name="Gaming Guild",
            status="active",
            category="gaming",
        )
    )
    await fed_repo.upsert_space(
        GlobalSpace(
            space_id="tech-sp",
            owning_instance="o.home",
            name="Tech Talk",
            status="active",
            category="tech",
        )
    )
    # ?category=gaming keeps only the gaming space.
    resp = await client.get("/?category=gaming")
    text = await resp.text()
    assert "Gaming Guild" in text
    assert "Tech Talk" not in text


async def test_landing_shows_category_label(client):
    app = client._app
    fed_repo = app[gfs_fed_repo_key]
    await fed_repo.upsert_instance(
        ClientInstance(
            instance_id="o.home",
            display_name="O",
            public_key="aa" * 32,
            inbox_url="http://o/wh",
            status="active",
        )
    )
    await fed_repo.upsert_space(
        GlobalSpace(
            space_id="outdoors-sp",
            owning_instance="o.home",
            name="Trail Crew",
            status="active",
            category="sports_outdoors",
        )
    )
    resp = await client.get("/")
    text = await resp.text()
    assert "Sports &amp; outdoors" in text or "Sports & outdoors" in text


async def test_landing_no_filter_shows_all_active(client):
    app = client._app
    fed_repo = app[gfs_fed_repo_key]
    await fed_repo.upsert_instance(
        ClientInstance(
            instance_id="o.home",
            display_name="O",
            public_key="aa" * 32,
            inbox_url="http://o/wh",
            status="active",
        )
    )
    await fed_repo.upsert_space(
        GlobalSpace(
            space_id="gaming-sp",
            owning_instance="o.home",
            name="Gaming Guild",
            status="active",
            category="gaming",
        )
    )
    await fed_repo.upsert_space(
        GlobalSpace(
            space_id="tech-sp",
            owning_instance="o.home",
            name="Tech Talk",
            status="active",
            category="tech",
        )
    )
    resp = await client.get("/")
    text = await resp.text()
    assert "Gaming Guild" in text
    assert "Tech Talk" in text


async def test_landing_renders_category_tabs(client):
    """The filter row renders an All tab + a per-category tab, and the
    ``.filters`` row uses ``flex-wrap`` so 10 tabs wrap on narrow screens."""
    resp = await client.get("/")
    text = await resp.text()
    assert 'href="/?category=gaming"' in text
    assert "Gaming" in text
    assert "flex-wrap" in text


async def test_landing_listing_rate_limit(client):
    """Spec §24.7.3: 30 GETs/min/IP on the public listing."""
    for _ in range(30):
        resp = await client.get("/")
        assert resp.status == 200
    resp = await client.get("/")
    assert resp.status == 429


# ─── Space page ───────────────────────────────────────────────────────


async def test_space_page_renders_deep_link(client):
    app = client._app
    fed_repo = app[gfs_fed_repo_key]
    await fed_repo.upsert_instance(
        ClientInstance(
            instance_id="o.home",
            display_name="O",
            public_key="aa" * 32,
            inbox_url="http://o/wh",
            status="active",
        )
    )
    await fed_repo.upsert_space(
        GlobalSpace(
            space_id="sp-deep",
            owning_instance="o.home",
            name="Deep Link Space",
            description="about",
            accent_color="#112233",
            status="active",
        )
    )
    resp = await client.get("/spaces/sp-deep")
    assert resp.status == 200
    text = await resp.text()
    assert "sh://join-space/http://gfs.test/spaces/sp-deep" in text
    assert 'property="og:title"' in text


async def test_space_page_renders_icon_and_brand_colors(client):
    """The per-space page renders the icon avatar + the space's real theme
    colours (primary), so the GFS page reflects the space's brand."""
    app = client._app
    fed_repo = app[gfs_fed_repo_key]
    await fed_repo.upsert_instance(
        ClientInstance(
            instance_id="o2.home",
            display_name="O2",
            public_key="bb" * 32,
            inbox_url="http://o2/wh",
            status="active",
        )
    )
    await fed_repo.upsert_space(
        GlobalSpace(
            space_id="sp-brand",
            owning_instance="o2.home",
            name="Branded",
            description="about",
            cover_url="data:image/webp;base64,Y292ZXI=",
            icon_url="data:image/webp;base64,aWNvbg==",
            accent_color="#445566",
            primary_color="#112233",
            status="active",
        )
    )
    resp = await client.get("/spaces/sp-brand")
    assert resp.status == 200
    text = await resp.text()
    assert 'class="space-avatar"' in text
    assert "data:image/webp;base64,aWNvbg==" in text  # icon
    assert "data:image/webp;base64,Y292ZXI=" in text  # cover
    assert "#112233" in text  # primary colour applied to --primary


async def test_space_page_404_for_pending_or_banned(client):
    app = client._app
    fed_repo = app[gfs_fed_repo_key]
    await fed_repo.upsert_instance(
        ClientInstance(
            instance_id="o.home",
            display_name="O",
            public_key="aa" * 32,
            inbox_url="http://o/wh",
            status="active",
        )
    )
    await fed_repo.upsert_space(
        GlobalSpace(
            space_id="sp-pending",
            owning_instance="o.home",
            name="Pending",
            status="pending",
        )
    )
    resp = await client.get("/spaces/sp-pending")
    assert resp.status == 404


# ─── Invite page ──────────────────────────────────────────────────────


async def test_invite_page_known_token(client):
    app = client._app
    fed_repo = app[gfs_fed_repo_key]
    admin_repo = app[gfs_admin_repo_key]
    await fed_repo.upsert_instance(
        ClientInstance(
            instance_id="o.home",
            display_name="O",
            public_key="aa" * 32,
            inbox_url="http://o/wh",
            status="active",
        )
    )
    await fed_repo.upsert_space(
        GlobalSpace(
            space_id="inv-sp",
            owning_instance="o.home",
            name="Invite Me",
            accent_color="#aabbcc",
            status="active",
        )
    )
    # Seed a valid invite-token row.
    await admin_repo._db.enqueue(
        "INSERT INTO gfs_invite_tokens(gfs_token, space_id, "
        "source_instance_id, max_uses) VALUES(?, ?, ?, ?)",
        ("invtok-1", "inv-sp", "o.home", 5),
    )
    resp = await client.get("/join/invtok-1")
    assert resp.status == 200
    text = await resp.text()
    assert "Invite Me" in text
    assert "sh://gfs-invite/http://gfs.test/join/invtok-1" in text


async def test_invite_page_unknown_token_404(client):
    resp = await client.get("/join/no-such-token")
    assert resp.status == 404


# ─── Pairing token rate-limit (spec §24.7.4) ──────────────────────────


async def test_pairing_token_rate_limit_per_ip(client):
    """A fresh token is issued on first visit; second visit within the
    30-second window gets a ``please-wait`` placeholder token instead.
    """
    resp = await client.get("/")
    assert resp.status == 200
    text_a = await resp.text()
    # Second immediate visit — rate-limited.
    resp = await client.get("/")
    text_b = await resp.text()
    assert "please-wait" in text_b
    # (The first token was real.)
    assert "please-wait" not in text_a


# ─── Pairing-code copy/paste fallback (socialhome:// scheme) ──────────


async def test_landing_renders_socialhome_pair_code(client):
    """The landing page renders a ``socialhome://gfs-pair/{base_url}
    ?token={token}`` pairing code in a ``<code>`` element next to the
    QR, plus a Copy-code button. Replaces the old ``sh://`` scheme
    that the SPA doesn't recognise."""
    resp = await client.get("/")
    assert resp.status == 200
    text = await resp.text()
    # New scheme present in the rendered HTML.
    assert "socialhome://gfs-pair/http://gfs.test?token=" in text
    # Old scheme is gone — operators who script against the landing
    # shouldn't get away with two parallel formats.
    assert "sh://gfs-pair" not in text
    # Copy button + the code id the inline JS shim wires to.
    assert 'id="pair-code"' in text
    assert 'id="copy-pair-btn"' in text


async def test_landing_pair_code_carries_token_in_data_attr(client):
    """The code element carries ``data-pair-token`` for tests + the
    inline JS shim — so screen-scraper tooling can extract the raw
    token without parsing the full URL."""
    resp = await client.get("/")
    text = await resp.text()
    assert "data-pair-token=" in text
