"""Three-instance federation demo + smoke test driver.

Boots three Social Home instances (Alpha / Beta / Gamma) in standalone
mode on adjacent ports, walks the §11 QR pairing handshake between
each pair, and then exercises the federation surface end-to-end:

* Posts, moments and highlights — verify that Alpha-side content shows
  up on Beta and Gamma.
* DMs — open a 1:1 conversation between users on different households
  and verify message round-trip.
* Spaces — Beta creates a space, mints remote-invites for Alpha and
  Gamma users, both accept; verify all three appear in the member list.
* WebRTC — runs against the real ``aiolibdatachannel`` transport
  (i.e. no ``SH_DISABLE_RTC=1`` fallback). The script aborts if any
  instance crashes during the run.

Usage::

    python .claude/skills/federation-demo/harness.py up      # boot + setup
    python .claude/skills/federation-demo/harness.py pair    # all pairwise pairings
    python .claude/skills/federation-demo/harness.py traffic # generate posts/moments/...
    python .claude/skills/federation-demo/harness.py verify  # assertions across all 3
    python .claude/skills/federation-demo/harness.py down    # stop + wipe data dirs
    python .claude/skills/federation-demo/harness.py all     # everything in order

State is persisted to ``/tmp/sh-demo/state.json`` so the steps can be
run independently. ``all`` is the canonical invocation.
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path("/tmp/sh-demo")
STATE_PATH = ROOT / "state.json"

# Each instance: (label, port, username, password, household_name).
# ``d`` is intentionally NOT directly paired with ``a`` — it pairs only
# with ``b`` so the harness can exercise §11 "simple pairing" via the
# transitive auto-pair-via flow (a → request_via(b, d) → d's admin
# approves → a ↔ d pair lands without a QR scan).
INSTANCES: tuple[tuple[str, int, str, str, str], ...] = (
    ("a", 18001, "alice", "alpha-pw", "Alpha House"),
    ("b", 18002, "bob", "beta-pw", "Beta House"),
    ("c", 18003, "carol", "gamma-pw", "Gamma House"),
    ("d", 18004, "dave", "delta-pw", "Delta House"),
)


# ─── State helpers ─────────────────────────────────────────────────────────


def _load() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text())
    return {}


def _save(state: dict) -> None:
    ROOT.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2))


# ─── HTTP helpers ──────────────────────────────────────────────────────────


def _request(
    url: str,
    *,
    token: str | None = None,
    method: str = "GET",
    body: dict | list | None = None,
    timeout: float = 15.0,
) -> tuple[int, Any]:
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8") or "{}"
            return r.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8")
        try:
            return exc.code, json.loads(raw)
        except Exception:
            return exc.code, {"_raw": raw}


def _must(
    label: str, status: int, body: Any, *, ok: tuple[int, ...] = (200, 201, 204)
) -> Any:
    if status not in ok:
        raise SystemExit(f"{label} failed: HTTP {status} body={body!r}")
    return body


def _upload_file(
    url: str,
    *,
    token: str,
    filename: str,
    content: bytes,
    content_type: str = "application/octet-stream",
    timeout: float = 30.0,
) -> tuple[int, Any]:
    """POST a single file as ``multipart/form-data`` and return ``(status, json)``.

    The harness's main :func:`_request` helper only handles JSON
    bodies, so the v_3 media-DM round-trip needs its own tiny
    multipart encoder. Built on stdlib so the demo stays
    dependency-free; the boundary is a fixed string because there
    are no user-controlled values to escape.
    """
    boundary = b"----shdemo-boundary"
    parts = [
        b"--" + boundary,
        b'Content-Disposition: form-data; name="file"; '
        b'filename="' + filename.encode("utf-8") + b'"',
        b"Content-Type: " + content_type.encode("utf-8"),
        b"",
        content,
        b"--" + boundary + b"--",
        b"",
    ]
    body = b"\r\n".join(parts)
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "multipart/form-data; boundary=" + boundary.decode(),
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8") or "{}"
            return r.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8")
        try:
            return exc.code, json.loads(raw)
        except Exception:
            return exc.code, {"_raw": raw}


def _make_demo_webp() -> bytes:
    """Return the bytes of a tiny solid-colour WebP for the media-DM test.

    Pillow ships with WebP support so we can produce real bytes the
    backend's :class:`ImageProcessor` won't reject. Kept small (16×16)
    to keep the demo's upload payload trivial — the federation
    pipeline is what we're exercising, not the image processor.
    """
    import io
    from PIL import Image

    img = Image.new("RGB", (16, 16), color=(200, 100, 50))
    buf = io.BytesIO()
    img.save(buf, format="WEBP", quality=80)
    return buf.getvalue()


# ─── Instance lifecycle ────────────────────────────────────────────────────


def _instance_dir(label: str) -> Path:
    return ROOT / label


def _write_config(label: str, port: int, name: str) -> None:
    d = _instance_dir(label)
    d.mkdir(parents=True, exist_ok=True)
    (d / "socialhome.toml").write_text(
        "[server]\n"
        'listen_host = "127.0.0.1"\n'
        f"listen_port = {port}\n"
        'log_level = "INFO"\n\n'
        "[storage]\n"
        f'data_dir = "{d}"\n\n'
        "[federation]\n"
        f'instance_name = "{name}"\n\n'
        "[standalone]\n"
        f'external_url = "http://127.0.0.1:{port}"\n'
    )


def _spawn(label: str, port: int) -> int:
    d = _instance_dir(label)
    log = open(d / "log.txt", "wb")
    env = {
        **os.environ,
        "SH_MODE": "standalone",
        "SH_CONFIG": str(d / "socialhome.toml"),
        "SH_LOG_LEVEL": "INFO",
    }
    p = subprocess.Popen(
        [sys.executable, "-u", "-m", "socialhome"],
        stdout=log,
        stderr=subprocess.STDOUT,
        env=env,
        start_new_session=True,
    )
    return p.pid


def _wait_ready(port: int, timeout: float = 30.0) -> dict:
    end = time.monotonic() + timeout
    last_err: Any = None
    while time.monotonic() < end:
        try:
            status, body = _request(
                f"http://127.0.0.1:{port}/api/instance/config", timeout=2.0
            )
            if status == 200:
                return body
        except Exception as exc:
            last_err = exc
        time.sleep(0.5)
    raise SystemExit(
        f"port {port} not ready after {timeout}s (last error: {last_err!r})"
    )


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


# ─── Step: up ──────────────────────────────────────────────────────────────


def cmd_up() -> None:
    """Wipe data dirs, write configs, boot all three instances, run /setup."""
    if ROOT.exists():
        shutil.rmtree(ROOT)
    ROOT.mkdir()

    state: dict = {"instances": {}}
    for label, port, user, pw, name in INSTANCES:
        _write_config(label, port, name)
        pid = _spawn(label, port)
        state["instances"][label] = {
            "port": port,
            "pid": pid,
            "username": user,
            "password": pw,
            "name": name,
        }
        print(f"  {label}: pid={pid} port={port}")

    print("waiting for instances...")
    for label, port, *_ in INSTANCES:
        cfg = _wait_ready(port)
        if not cfg.get("setup_required"):
            raise SystemExit(f"{label}: setup_required=false on a fresh data dir")

    for label, port, user, pw, name in INSTANCES:
        status, body = _request(
            f"http://127.0.0.1:{port}/api/setup/standalone",
            method="POST",
            body={"username": user, "password": pw, "household_name": name},
        )
        body = _must(f"setup({label})", status, body, ok=(201,))
        state["instances"][label]["token"] = body["token"]
        # Capture instance_id by querying friends — the "instance" key carries it.
        s2, fr = _request(
            f"http://127.0.0.1:{port}/api/friends",
            token=body["token"],
        )
        _must(f"friends({label})", s2, fr)
        state["instances"][label]["instance_id"] = fr["instance"]["instance_id"]
        # And user_id for the admin user.
        s3, me = _request(
            f"http://127.0.0.1:{port}/api/me",
            token=body["token"],
        )
        _must(f"me({label})", s3, me)
        state["instances"][label]["user_id"] = me["user_id"]
        print(f"  {label}: instance_id={state['instances'][label]['instance_id']}")

    _save(state)
    print("up: ok")


# ─── Step: gfs-up ──────────────────────────────────────────────────────────

GFS_PORT = 18765
GFS_DIR = ROOT / "gfs"


def _gfs_config_path() -> Path:
    return GFS_DIR / "global_server.toml"


def _gfs_alive(state: dict) -> bool:
    pid = (state.get("gfs") or {}).get("pid")
    return bool(pid and _alive(pid))


def cmd_gfs_up() -> None:
    """Start a Global Federation Server (GFS) on ``127.0.0.1:18765``.

    Uses ``socialhome-global-server`` (the ``socialhome[global-server]``
    console script) under the hood, but bypasses the interactive
    ``--init`` / ``--set-password`` CLI: we write the example TOML
    directly, set ``[server] base_url`` to the loopback URL, and seed
    the bcrypt admin-password hash via :func:`set_password_in_toml` so
    the harness can boot the GFS in one shot.

    Prerequisite: ``cmd_up`` must have run so ``/tmp/sh-demo`` exists.
    """
    state = _load()
    if not state:
        raise SystemExit("run 'up' first")

    # Lazy import — these helpers only ship when the project is
    # installed editable; the rest of the harness doesn't need them.
    from socialhome.global_server.admin import hash_password
    from socialhome.global_server.config import (
        set_password_in_toml,
        write_example_config,
    )

    GFS_DIR.mkdir(parents=True, exist_ok=True)
    config_path = _gfs_config_path()
    if not config_path.exists():
        write_example_config(config_path)
    # Patch the config in-place so the loopback start-up succeeds.
    text = config_path.read_text(encoding="utf-8")
    text = text.replace(
        'host     = "0.0.0.0"',
        'host     = "127.0.0.1"',
    )
    text = text.replace(
        "port     = 8765",
        f"port     = {GFS_PORT}",
    )
    text = text.replace(
        'base_url = "https://gfs.example.com"',
        f'base_url = "http://127.0.0.1:{GFS_PORT}"',
    )
    text = text.replace(
        'data_dir = "/var/lib/sh-gfs"',
        f'data_dir = "{GFS_DIR}"',
    )
    config_path.write_text(text, encoding="utf-8")
    set_password_in_toml(config_path, hash_password("gfs-admin-pw"))

    log = open(GFS_DIR / "log.txt", "wb")
    # ``socialhome.global_server.server`` has no ``__main__`` guard, so
    # ``python -m`` imports the module without calling ``main()``. Use
    # ``-c`` to invoke ``main()`` directly. ``sys.argv`` inside the
    # subprocess starts with ``-c`` then our forwarded args.
    p = subprocess.Popen(
        [
            sys.executable,
            "-u",
            "-c",
            "from socialhome.global_server.server import main; main()",
            "--config",
            str(config_path),
        ],
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    deadline = time.monotonic() + 30.0
    last_err: Any = None
    while time.monotonic() < deadline:
        try:
            s, _ = _request(f"http://127.0.0.1:{GFS_PORT}/healthz", timeout=2.0)
            if s == 200:
                break
        except Exception as exc:
            last_err = exc
        time.sleep(0.5)
    else:
        raise SystemExit(f"GFS not ready after 30 s (last error: {last_err!r})")
    state["gfs"] = {
        "pid": p.pid,
        "port": GFS_PORT,
        "base_url": f"http://127.0.0.1:{GFS_PORT}",
        "config_path": str(config_path),
        "admin_password": "gfs-admin-pw",
    }
    _save(state)
    print(f"  gfs: pid={p.pid} port={GFS_PORT} healthz=200")
    print("gfs-up: ok")


def _gfs_mint_pair_token() -> str:
    """Hit the GFS landing page from a fresh-looking IP and pull the
    one-time pair token out of the rendered HTML.

    The token is also embedded in the QR PNG, but the landing page
    renders it as a copyable string in the right column too — easier
    to scrape from a script than the PNG.
    """
    # Use a unique X-Forwarded-For so the per-IP rate limiter doesn't
    # gate test reruns.
    ip_marker = f"127.{secrets.randbelow(255)}.0.{secrets.randbelow(255)}"
    req = urllib.request.Request(
        f"http://127.0.0.1:{GFS_PORT}/",
        headers={"X-Forwarded-For": ip_marker},
    )
    with urllib.request.urlopen(req, timeout=5) as r:
        html = r.read().decode("utf-8")
    # The landing template renders ``token`` directly in the page
    # body (in a copy-friendly token block) so we can pull it out
    # with a simple substring search. Fall back to None — the harness
    # surfaces a clearer error than a random KeyError that way.
    marker = 'data-pair-token="'
    idx = html.find(marker)
    if idx < 0:
        # Older template: the token is rendered inside a ``<code>`` on
        # the QR card.
        for needle in ('id="pair-token">', 'class="pair-token">'):
            j = html.find(needle)
            if j >= 0:
                start = j + len(needle)
                end = html.find("<", start)
                tok = html[start:end].strip()
                if tok:
                    return tok
        raise SystemExit(
            "could not extract pair token from GFS landing page —"
            " template format may have changed",
        )
    start = idx + len(marker)
    end = html.find('"', start)
    return html[start:end]


def cmd_gfs_pair() -> None:
    """Pair Alpha + Delta with the GFS.

    Walk the §24 GFS pairing flow end-to-end:

    1. Mint a one-time pair token via the GFS landing page (rendered
       at ``GET /`` — the same page that displays the QR code).
    2. POST it to Alpha's ``/api/gfs/connections`` so Alpha runs
       :meth:`GfsConnectionService.pair` (fetch ``GET /gfs/info``,
       then ``POST /gfs/register`` with Alpha's identity + the
       token).
    3. Repeat for Delta with a fresh token.
    4. Assert both households now show the GFS connection as
       ``status="active"`` (auto-accept is on by default for fresh
       deployments).
    """
    state = _load()
    if not state or not _gfs_alive(state):
        raise SystemExit("run 'gfs-up' first")

    a = state["instances"]["a"]
    d = state["instances"]["d"]
    gfs_url = f"http://127.0.0.1:{GFS_PORT}"

    state.setdefault("gfs", {})
    state["gfs"]["pairings"] = {}
    for label, info in (("a", a), ("d", d)):
        token = _gfs_mint_pair_token()
        s, resp = _request(
            f"http://127.0.0.1:{info['port']}/api/gfs/connections",
            token=info["token"],
            method="POST",
            body={"gfs_url": gfs_url, "token": token},
        )
        _must(f"gfs-pair({label})", s, resp, ok=(201,))
        state["gfs"]["pairings"][label] = {
            "id": resp["id"],
            "gfs_instance_id": resp["gfs_instance_id"],
            "status": resp["status"],
        }
        print(
            f"  {label}: paired with GFS — id={resp['id'][:8]} "
            f"status={resp['status']}"
        )
        if resp["status"] != "active":
            raise SystemExit(
                f"{label}: expected GFS connection status=active, got"
                f" {resp['status']!r}",
            )
    _save(state)
    print("gfs-pair: ok (a + d connected to GFS)")


def cmd_gfs_traffic() -> None:
    """Exercise the global-space publish path against the running GFS.

    1. Alpha creates a ``space_type=global`` space. The
       ``_auto_publish_on_type`` hook on ``SpaceService`` fans out a
       signed publish call to every paired GFS.
    2. The harness polls ``GET /gfs/spaces`` on the GFS until the new
       space appears with ``status="active"``. Asserts the published
       metadata (name, owning_instance) matches what Alpha sent.

    This validates the publish wire end-to-end (SH-side ``publish_space``
    → POST ``/gfs/spaces/{id}/publish`` → GFS verifies the Ed25519
    signature against the registered ``ClientInstance.public_key`` →
    ``upsert_space`` row → ``list_spaces``). The downstream join /
    SPACE_POST_CREATED relay path is still TODO — see SKILL.md.
    """
    state = _load()
    if not state or not _gfs_alive(state):
        raise SystemExit("run 'gfs-up' + 'gfs-pair' first")

    a = state["instances"]["a"]
    space_name = "Global Test Space"
    s, body = _request(
        f"http://127.0.0.1:{a['port']}/api/spaces",
        token=a["token"],
        method="POST",
        body={
            "name": space_name,
            "description": "harness end-to-end probe for GFS publish",
            "space_type": "global",
        },
    )
    body = _must("create-global-space", s, body, ok=(201,))
    space_id = body["id"]
    print(f"  a: created global space {space_id}")
    state.setdefault("gfs", {})["global_space_id"] = space_id
    _save(state)

    deadline = time.monotonic() + 30.0
    listing: list[dict] = []
    while time.monotonic() < deadline:
        s, payload = _request(f"http://127.0.0.1:{GFS_PORT}/gfs/spaces")
        if s == 200:
            listing = payload.get("spaces", []) if isinstance(payload, dict) else []
            if any(sp["space_id"] == space_id for sp in listing):
                break
        time.sleep(1.0)
    match = [sp for sp in listing if sp["space_id"] == space_id]
    if not match:
        raise SystemExit(
            f"gfs-traffic: space {space_id} did not appear on GET /gfs/spaces"
            f" within 30s — listing was {listing!r}",
        )
    sp = match[0]
    if sp["name"] != space_name:
        raise SystemExit(
            f"gfs-traffic: GFS listed name={sp['name']!r}, expected"
            f" {space_name!r}",
        )
    if sp["owning_instance"] != a["instance_id"]:
        raise SystemExit(
            f"gfs-traffic: GFS listed owning_instance={sp['owning_instance']!r},"
            f" expected {a['instance_id']!r}",
        )
    print(
        f"  gfs lists '{sp['name']}' "
        f"(owner {sp['owning_instance'][:8]}…, status {sp['status']}) ✓"
    )
    print("gfs-traffic: ok (publish round-trip)")


def cmd_gfs_replay() -> None:
    """Validate GFS-paired state survives an HFS restart.

    Sequence:
    1. Pre-check that the GFS lists Alpha's published global space and
       Alpha's local ``/api/gfs/publications`` shows the same row
       (i.e. the publish from :func:`cmd_gfs_traffic` already landed).
    2. SIGTERM Alpha and wait for the process to exit.
    3. While Alpha is down, the GFS still lists the space — the owning
       HFS being unreachable is not a deregistration signal. Asserted.
    4. Respawn Alpha on the same data_dir; wait for
       ``/api/instance/config`` to answer 200.
    5. Wait across the GFS WS supervisor reconcile interval + the
       ``GfsWebSocketClient`` first-connect window so the supervisor's
       background loop reopens ``wss://gfs/gfs/ws`` against the GFS.
    6. Re-assert: Alpha's ``/api/gfs/connections`` still shows the
       connection active, ``/api/gfs/publications`` still lists the
       global space, and the GFS continues to list it on
       ``GET /gfs/spaces``.

    Prereqs (chain via ``up`` → ``gfs-up`` → ``gfs-pair`` →
    ``gfs-traffic``).
    """
    state = _load()
    if not state:
        raise SystemExit("run 'up' first")
    if not _gfs_alive(state):
        raise SystemExit("run 'gfs-up' first")
    gfs = state.get("gfs") or {}
    global_space_id = gfs.get("global_space_id")
    if not global_space_id:
        raise SystemExit("run 'gfs-traffic' first — needs Alpha's global space")

    a = state["instances"]["a"]
    a_token = a["token"]
    gfs_url = f"http://127.0.0.1:{GFS_PORT}"
    pairings = (state.get("gfs") or {}).get("pairings") or {}
    alpha_pairing = pairings.get("a")
    if not alpha_pairing:
        raise SystemExit("run 'gfs-pair' first — Alpha must be paired")
    # ``cmd_gfs_pair`` stashes the local SH-side ``GfsConnection.id``
    # (UUID generated when Alpha called ``POST /api/gfs/connections``)
    # under ``id``. That's the row Alpha looks up via
    # ``/api/gfs/connections``; the GFS-side instance id is separate.
    gfs_conn_id = alpha_pairing["id"]

    # 1a. Pre-check — GFS lists the space.
    s, payload = _request(f"{gfs_url}/gfs/spaces")
    _must("gfs-replay: pre /gfs/spaces", s, payload)
    listing = payload.get("spaces", []) if isinstance(payload, dict) else []
    if not any(sp["space_id"] == global_space_id for sp in listing):
        raise SystemExit(
            f"gfs-replay precheck: GFS {gfs_url} did not list "
            f"{global_space_id} before Alpha shutdown — listing={listing!r}",
        )
    print(f"  pre-check: GFS lists {global_space_id[:8]}… ✓")

    # 1b. Pre-check — Alpha's local publications mirror.
    def _alpha_publications() -> list[dict]:
        s, body = _request(
            f"http://127.0.0.1:{a['port']}/api/gfs/publications",
            token=a_token,
        )
        _must("gfs-replay: /api/gfs/publications", s, body)
        return list(body.get("publications") or [])

    pubs = _alpha_publications()
    if not any(p.get("space_id") == global_space_id for p in pubs):
        raise SystemExit(
            f"gfs-replay precheck: Alpha's /api/gfs/publications missing "
            f"{global_space_id} — got {pubs!r}",
        )
    print(f"  pre-check: Alpha sees publication for {global_space_id[:8]}… ✓")

    # 2. Tear Alpha down (SIGTERM, then SIGKILL after grace) — process
    #    group so libdatachannel + the GFS WS background task exit too.
    print(f"  killing a (pid={a['pid']}) to simulate owning-HFS downtime")
    try:
        os.killpg(a["pid"], signal.SIGTERM)
    except ProcessLookupError:
        print("  a was already gone")
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline and _alive(a["pid"]):
        time.sleep(0.2)
    if _alive(a["pid"]):
        try:
            os.killpg(a["pid"], signal.SIGKILL)
        except ProcessLookupError:
            pass
        time.sleep(0.5)

    # 3. While Alpha is offline, GFS keeps the row. (The GFS does not
    #    proactively unpublish on owning-instance disconnect — that
    #    would create a thundering-herd republish whenever a flock of
    #    HFSes restart in concert.)
    s, payload = _request(f"{gfs_url}/gfs/spaces")
    _must("gfs-replay: /gfs/spaces while a is down", s, payload)
    listing = payload.get("spaces", []) if isinstance(payload, dict) else []
    if not any(sp["space_id"] == global_space_id for sp in listing):
        raise SystemExit(
            f"gfs-replay: GFS dropped {global_space_id} while owning HFS "
            f"was offline — listing={listing!r}",
        )
    print(f"  during downtime: GFS still lists {global_space_id[:8]}… ✓")

    # 4. Respawn Alpha on the same port + data_dir.
    new_pid = _spawn("a", a["port"])
    state["instances"]["a"]["pid"] = new_pid
    _wait_ready(a["port"])
    print(f"  a respawned: pid={new_pid} ready=200")

    # 5. Settle the GFS WS supervisor reconnect. Worst case is one
    #    reconcile-loop pass (~5 s) + one WS reconnect-delay slot
    #    (1 s default). 8 s is generous for the WS hello to land.
    settle = 8
    print(f"  waiting {settle}s for GFS WS supervisor to reconnect…")
    time.sleep(settle)

    # 6a. Alpha's local connection row still active.
    s, conns = _request(
        f"http://127.0.0.1:{a['port']}/api/gfs/connections",
        token=a_token,
    )
    _must("gfs-replay: /api/gfs/connections", s, conns)
    rows = conns if isinstance(conns, list) else []
    match = [c for c in rows if c.get("id") == gfs_conn_id]
    if not match or match[0].get("status") != "active":
        raise SystemExit(
            f"gfs-replay: Alpha's connection {gfs_conn_id} not active after "
            f"restart — got {rows!r}",
        )
    print(f"  post-restart: Alpha connection {gfs_conn_id[:8]}… status=active ✓")

    # 6b. Alpha's publication mirror survived the restart.
    pubs = _alpha_publications()
    if not any(p.get("space_id") == global_space_id for p in pubs):
        raise SystemExit(
            f"gfs-replay: Alpha's /api/gfs/publications dropped "
            f"{global_space_id} across the restart — got {pubs!r}",
        )
    print(f"  post-restart: Alpha sees publication for {global_space_id[:8]}… ✓")

    # 6c. GFS still lists Alpha's space.
    s, payload = _request(f"{gfs_url}/gfs/spaces")
    _must("gfs-replay: /gfs/spaces post-restart", s, payload)
    listing = payload.get("spaces", []) if isinstance(payload, dict) else []
    if not any(sp["space_id"] == global_space_id for sp in listing):
        raise SystemExit(
            f"gfs-replay: GFS lost the space after Alpha restart — "
            f"listing={listing!r}",
        )
    print(f"  post-restart: GFS still lists {global_space_id[:8]}… ✓")

    state["gfs_replay_ran"] = True
    _save(state)
    print("gfs-replay: ok (publication survives owning-HFS downtime)")


def cmd_gfs_down() -> None:
    """Stop the GFS started by :func:`cmd_gfs_up` (idempotent)."""
    state = _load()
    gfs = state.get("gfs")
    if not gfs:
        return
    try:
        os.killpg(gfs["pid"], signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass
    time.sleep(1)
    try:
        os.killpg(gfs["pid"], signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    state.pop("gfs", None)
    _save(state)
    print("gfs-down: ok")


# ─── Step: pair ────────────────────────────────────────────────────────────


def _pair_two(state: dict, initiator: str, scanner: str) -> None:
    a = state["instances"][initiator]
    b = state["instances"][scanner]

    s, qr = _request(
        f"http://127.0.0.1:{a['port']}/api/pairing/initiate",
        token=a["token"],
        method="POST",
    )
    _must(f"initiate({initiator})", s, qr, ok=(201,))
    s, ack = _request(
        f"http://127.0.0.1:{b['port']}/api/pairing/accept",
        token=b["token"],
        method="POST",
        body=qr,
    )
    _must(f"accept({scanner})", s, ack)
    s, conf = _request(
        f"http://127.0.0.1:{a['port']}/api/pairing/confirm",
        token=a["token"],
        method="POST",
        body={"token": ack["token"], "verification_code": ack["verification_code"]},
    )
    _must(f"confirm({initiator})", s, conf)
    print(f"  paired {initiator} ↔ {scanner}")


def cmd_pair() -> None:
    """Pair the inner ring (a/b/c) pairwise, plus d↔b.

    ``d`` deliberately stays unpaired with ``a`` — :func:`cmd_relay_pair`
    finishes the job through the §11 trust-relay flow.
    """
    state = _load()
    if not state:
        raise SystemExit("run 'up' first")

    for initiator, scanner in (
        ("a", "b"),
        ("b", "c"),
        ("a", "c"),
        ("b", "d"),
    ):
        _pair_two(state, initiator, scanner)

    # Settle: peer-confirm + initial peer-directory snapshots.
    time.sleep(3)
    expected = {
        "a": 2,  # b, c
        "b": 3,  # a, c, d
        "c": 2,  # a, b
        "d": 1,  # b only — a-via-relay lands later
    }
    for label, info in state["instances"].items():
        s, conns = _request(
            f"http://127.0.0.1:{info['port']}/api/pairing/connections",
            token=info["token"],
        )
        _must(f"connections({label})", s, conns)
        confirmed = [c for c in conns if c["status"] == "confirmed"]
        if len(confirmed) != expected[label]:
            raise SystemExit(
                f"{label}: expected {expected[label]} confirmed peers, "
                f"got {len(confirmed)} "
                f"({[c['display_name'] for c in conns]})"
            )
    print("pair: ok (a↔b, b↔c, a↔c, b↔d)")


def cmd_relay_pair() -> None:
    """Auto-pair a ↔ d via b (§11 simple-pairing / trust-relay flow).

    1. Alpha asks Beta to vouch for an introduction to Delta:
       ``POST /api/pairing/auto-pair-via {via_instance_id, target_instance_id}``.
       Beta forwards the request to Delta over federation — no admin
       click needed on Beta's side.
    2. Delta's admin sees the pending request in
       ``GET /api/pairing/auto-pair-requests`` and approves it via
       ``POST /api/pairing/auto-pair-requests/{id}/approve`` —
       one-click, no QR scan.
    3. After approval the pair lands on both Alpha and Delta as
       ``CONFIRMED`` and the peer-directory snapshot kicks in.

    The QR step in :func:`cmd_pair` already burned much of Alpha's
    ``/api/pairing/*`` rate-limit budget (5 / 60 s per user); we wait
    for the bucket to drain before issuing the auto-pair-via. Without
    this the very first request returns 429.
    """
    state = _load()
    if not state:
        raise SystemExit("run 'up' first")

    a = state["instances"]["a"]
    d = state["instances"]["d"]
    b = state["instances"]["b"]

    print("  waiting 65 s for /api/pairing/* rate-limit window to drain...")
    time.sleep(65)

    s, _resp = _request(
        f"http://127.0.0.1:{a['port']}/api/pairing/auto-pair-via",
        token=a["token"],
        method="POST",
        body={
            "via_instance_id": b["instance_id"],
            "target_instance_id": d["instance_id"],
            "target_display_name": d["name"],
        },
    )
    _must("auto-pair-via(a→d)", s, _resp, ok=(202,))
    print(f"  a → request_via(b, d): 202 {_resp}")

    # Give the federated request a beat to land in d's inbox. Polling
    # interval is 2 s — the auto-pair-requests endpoint is rate-limited
    # so faster polls trip 429.
    deadline = time.monotonic() + 30.0
    request_id: str | None = None
    while time.monotonic() < deadline:
        s, inbox = _request(
            f"http://127.0.0.1:{d['port']}/api/pairing/auto-pair-requests",
            token=d["token"],
        )
        if s == 200:
            items = inbox if isinstance(inbox, list) else (inbox.get("items") or [])
            if items:
                request_id = items[0]["request_id"]
                break
        time.sleep(2.0)
    if request_id is None:
        raise SystemExit("d's auto-pair inbox stayed empty after 30s")

    s, _resp = _request(
        f"http://127.0.0.1:{d['port']}/api/pairing/auto-pair-requests/"
        f"{request_id}/approve",
        token=d["token"],
        method="POST",
    )
    _must("auto-pair approve(d)", s, _resp)
    print(f"  d approves request {request_id}")

    # Settle and assert both ends now see each other CONFIRMED.
    time.sleep(3)
    for label, peer in (("a", d["instance_id"]), ("d", a["instance_id"])):
        info = state["instances"][label]
        s, conns = _request(
            f"http://127.0.0.1:{info['port']}/api/pairing/connections",
            token=info["token"],
        )
        _must(f"connections({label})", s, conns)
        match = [c for c in conns if c["instance_id"] == peer]
        if not match or match[0]["status"] != "confirmed":
            raise SystemExit(f"{label} → {peer[:8]}: expected confirmed, got {match!r}")
    state["relay_pair_ran"] = True
    _save(state)
    print("relay-pair: ok (a ↔ d confirmed via b)")


# ─── Step: traffic ─────────────────────────────────────────────────────────


def cmd_traffic() -> None:
    """Post one item of each public type from every household."""
    state = _load()
    if not state:
        raise SystemExit("run 'up' first")

    # Momentum follows (a → b, c → b) are issued first so Beta's moment
    # later in the loop fans out to Alpha + Carol as inbox recipients.
    # The follow itself federates as ``USER_FOLLOW`` and Beta's
    # ``moment_follows`` mirror picks it up before the moment is posted.
    state["moment_follows"] = {}
    for follower_label in ("a", "c"):
        follower = state["instances"][follower_label]
        s, _r = _request(
            f"http://127.0.0.1:{follower['port']}/api/moments/follows",
            token=follower["token"],
            method="POST",
            body={
                "user_id": state["instances"]["b"]["user_id"],
                "instance_id": state["instances"]["b"]["instance_id"],
            },
        )
        # 200/201/204 all valid (depending on follow-create vs idempotent
        # re-follow); 409 means we're already following from a prior run
        # of ``traffic`` against a re-used /tmp/sh-demo.
        if s in (200, 201, 204, 409):
            state["moment_follows"][follower_label] = "b"
            print(f"  {follower_label} now follows b on momentum")
        else:
            print(f"  {follower_label} → b follow FAILED: {s} {_r}")
    # Settle the follow before any household posts a moment so Beta's
    # ``moment_follows`` mirror is populated when ``MOMENT_CREATED``
    # fans out.
    if state["moment_follows"]:
        time.sleep(2)

    state.setdefault("moments", {})
    for label, info in state["instances"].items():
        port, token, user = info["port"], info["token"], info["username"]
        url = f"http://127.0.0.1:{port}"

        s, _ = _request(
            f"{url}/api/me",
            token=token,
            method="PATCH",
            body={
                "display_name": f"{user.title()} ({info['name']})",
                "bio": f"Hello from {info['name']}",
            },
        )
        _must(f"profile({label})", s, _)

        s, _ = _request(
            f"{url}/api/feed/posts",
            token=token,
            method="POST",
            body={"content": f"[{label}] post — visible only inside {info['name']}"},
        )
        _must(f"post({label})", s, _, ok=(201,))

        # Moment audience defaults to households in the schema; rate-limited
        # to one per 15min, so a single moment per instance is all we need.
        moment_content = f"🌅 [{label}] moment from {info['name']}"
        s, m_resp = _request(
            f"{url}/api/moments",
            token=token,
            method="POST",
            body={"content": moment_content},
        )
        _must(f"moment({label})", s, m_resp, ok=(201, 429))
        if s == 201:
            # Strip the verify-side signature wrapper if it exists; the
            # signed payload nests the moment under ``data``.
            mp = m_resp.get("data") if isinstance(m_resp.get("data"), dict) else m_resp
            state["moments"][label] = {
                "id": mp.get("id"),
                "content": moment_content,
            }

        s, _ = _request(
            f"{url}/api/highlights/frames",
            token=token,
            method="POST",
            body={
                "media_url": "https://example.invalid/img.jpg",
                "frame_type": "image",
                "caption_text": f"[{label}] highlight — audience all_paired",
                "audience_kind": "all_paired",
            },
        )
        _must(f"highlight({label})", s, _, ok=(201,))

        print(f"  {label}: profile + post + moment + highlight queued")

    # Cross-household DMs: a → c (transit through pairwise federation).
    a = state["instances"]["a"]
    c = state["instances"]["c"]
    # Cross-household DM uses ``user_id`` so the DM service can resolve
    # Carol from her ``remote_users`` row (mirrored on Alpha when the
    # peer-directory snapshot from Gamma landed); ``username`` is
    # local-only.
    s, conv = _request(
        f"http://127.0.0.1:{a['port']}/api/conversations/dm",
        token=a["token"],
        method="POST",
        body={"user_id": c["user_id"]},
    )
    if s in (200, 201):
        state["dm_a_to_c"] = conv["id"]
        s, msg = _request(
            f"http://127.0.0.1:{a['port']}/api/conversations/{conv['id']}/messages",
            token=a["token"],
            method="POST",
            body={"content": "[a→c] hello carol from alice"},
        )
        if s in (200, 201):
            state["dm_msg_id"] = msg.get("id")
            print(f"  a→c DM created: conv={conv['id']}, msg={state['dm_msg_id']}")
        else:
            print(f"  a→c DM message FAILED: {s} {msg}")

        # v_3 cross-household media DM (a → c): upload a tiny WebP
        # via ``/api/media/upload``, send it as ``type='image'`` in
        # a follow-up DM, stash the message id. The verify step
        # checks that Carol's instance receives the preview
        # immediately + then the full bytes via DM_MEDIA_BLOB.
        webp_bytes = _make_demo_webp()
        s, up = _upload_file(
            f"http://127.0.0.1:{a['port']}/api/media/upload",
            token=a["token"],
            filename="alice.webp",
            content=webp_bytes,
            content_type="image/webp",
        )
        if s in (200, 201):
            s, mmsg = _request(
                f"http://127.0.0.1:{a['port']}/api/conversations/{conv['id']}/messages",
                token=a["token"],
                method="POST",
                body={
                    "type": "image",
                    "media_url": up["url"],
                    "file_name": "alice.webp",
                    "mime_type": "image/webp",
                    "file_size_bytes": len(webp_bytes),
                    "content": "",
                },
            )
            if s in (200, 201):
                state["dm_media_msg_id"] = mmsg.get("id")
                print(
                    f"  a→c media DM created: msg={state['dm_media_msg_id']} "
                    f"({len(webp_bytes)} bytes)",
                )
            else:
                print(f"  a→c media DM FAILED: {s} {mmsg}")
        else:
            print(f"  a→c media upload FAILED: {s} {up}")
    else:
        print(f"  a→c DM SKIPPED (create returned {s} {conv})")

    # Beta creates a space and invites alice + carol via remote-invite.
    b = state["instances"]["b"]
    s, space = _request(
        f"http://127.0.0.1:{b['port']}/api/spaces",
        token=b["token"],
        method="POST",
        body={
            "name": "Tri-household salon",
            "description": "All three houses",
            "join_mode": "invite_only",
        },
    )
    _must("space create(b)", s, space, ok=(201,))
    state["space_id"] = space["id"]
    print(f"  b: space {space['id']} created")

    for guest_label in ("a", "c"):
        guest = state["instances"][guest_label]
        s, inv = _request(
            f"http://127.0.0.1:{b['port']}/api/spaces/{space['id']}/remote-invites",
            token=b["token"],
            method="POST",
            body={
                "invitee_instance_id": guest["instance_id"],
                "invitee_user_id": guest["user_id"],
            },
        )
        _must(f"remote-invite({guest_label})", s, inv, ok=(201,))
        print(f"  b → {guest_label}: invite token issued")

    # Bazaar listing in the salon space — Beta posts a fixed-price item
    # to validate the bazaar surface boots inside a fresh space, and to
    # give Alpha something concrete to inquire about over DM in the
    # next step. The listing itself stays HFS-local (bazaar listings
    # are space-scoped); the *DM about the listing* is the federation
    # path under test.
    listing_title = "Vintage moka pot — barely used"
    s, listing = _request(
        f"http://127.0.0.1:{b['port']}/api/bazaar",
        token=b["token"],
        method="POST",
        body={
            "space_id": space["id"],
            "title": listing_title,
            "description": "Three-cup, brass-coloured. Pickup or shipping.",
            "mode": "fixed",
            "currency": "EUR",
            "price": 1500,
            "duration_days": 30,
        },
    )
    _must("bazaar create(b)", s, listing, ok=(201,))
    # Bazaar listings are keyed by ``post_id`` (not ``id``) — the
    # listing row composes onto the underlying post and shares its
    # primary key. The DM body quotes this value so verify can grep
    # for it on Beta's inbox.
    listing_id = listing["post_id"]
    state["bazaar_listing_id"] = listing_id
    state["bazaar_listing_title"] = listing_title
    print(f"  b: bazaar listing {listing_id[:8]}… created in salon")

    # Alpha → Beta DM *about the bazaar listing*. Bazaar's own DM-the-
    # seller flow is a SPA convenience (deep-link to the conversations
    # tab); on the wire it's a regular DM message that mentions the
    # listing id. We exercise the cross-household DM path: Alpha
    # creates the conversation against Beta's user_id (via federation
    # routing through the b↔a peer link) and posts a text message
    # quoting the listing title + id so the verify step has a stable
    # needle to grep for in Beta's inbox.
    s, conv_ab = _request(
        f"http://127.0.0.1:{a['port']}/api/conversations/dm",
        token=a["token"],
        method="POST",
        body={"user_id": b["user_id"]},
    )
    if s in (200, 201):
        state["dm_a_to_b"] = conv_ab["id"]
        msg_body = (
            f"[a→b] hi Bob — interested in your bazaar listing "
            f"{listing_id} ({listing_title!r}). still available?"
        )
        s, msg = _request(
            f"http://127.0.0.1:{a['port']}/api/conversations/{conv_ab['id']}/messages",
            token=a["token"],
            method="POST",
            body={"content": msg_body},
        )
        if s in (200, 201):
            state["dm_a_to_b_body"] = msg_body
            print(
                f"  a→b bazaar DM created: conv={conv_ab['id']}, "
                f"msg={msg.get('id')}"
            )
        else:
            print(f"  a→b bazaar DM message FAILED: {s} {msg}")
    else:
        print(f"  a→b bazaar DM SKIPPED (create returned {s} {conv_ab})")

    _save(state)


def cmd_calendar() -> None:
    """Cross-household space calendar + RSVP federation.

    Prereqs (run :func:`cmd_traffic` first):
    * Beta has a private space with pending remote-invites for Alice
      and Carol on it.

    Sequence:
    1. Alpha and Gamma fetch the inbound invite tokens via
       ``GET /api/remote_invites`` and accept them
       (``POST /api/remote_invites/{token}/accept``). Both households
       become space members on Beta's side.
    2. Beta creates a calendar event in the space
       (``POST /api/spaces/{id}/calendar/events``). The event
       federates as ``SPACE_CALENDAR_EVENT_CREATED`` to Alpha and Gamma.
    3. Alpha and Carol RSVP "going"
       (``POST /api/calendars/events/{id}/rsvp``). The RSVP federates
       back to Beta as ``SPACE_CALENDAR_RSVP``.
    4. Verify on Beta:
       ``GET /api/calendars/events/{id}/rsvps`` returns both Alpha's
       and Carol's user_ids with status="going".
    """
    state = _load()
    if not state:
        raise SystemExit("run 'up' first")
    if "space_id" not in state:
        raise SystemExit("run 'traffic' first — needs Beta's tri-household space")

    b = state["instances"]["b"]
    space_id = state["space_id"]

    # 1. Each guest accepts their pending remote-invite.
    for guest_label in ("a", "c"):
        guest = state["instances"][guest_label]
        s, invites = _request(
            f"http://127.0.0.1:{guest['port']}/api/remote_invites",
            token=guest["token"],
        )
        _must(f"remote_invites({guest_label})", s, invites)
        items = invites if isinstance(invites, list) else (invites.get("items") or [])
        target = next(
            (i for i in items if i.get("space_id") == space_id),
            None,
        )
        if target is None:
            raise SystemExit(
                f"{guest_label} has no pending invite for space {space_id}",
            )
        token = target["invite_token"]
        s, _r = _request(
            f"http://127.0.0.1:{guest['port']}/api/remote_invites/{token}/accept",
            token=guest["token"],
            method="POST",
        )
        _must(f"accept-invite({guest_label})", s, _r, ok=(204,))
        print(f"  {guest_label} accepted invite for space {space_id}")

    # Settle: SPACE_PRIVATE_INVITE_ACCEPT round-trips back to Beta and
    # seats the guest as a remote member.
    time.sleep(3)

    # 2. Beta creates a calendar event in the space — with an explicit
    #    IANA ``tz`` so the demo can assert the new v2 field rides
    #    through ``SPACE_CALENDAR_EVENT_CREATED`` to Alpha and Carol.
    start = "2027-01-15T18:00:00+00:00"
    end = "2027-01-15T20:00:00+00:00"
    s, ev = _request(
        f"http://127.0.0.1:{b['port']}/api/spaces/{space_id}/calendar/events",
        token=b["token"],
        method="POST",
        body={
            "summary": "Tri-household tabletop night",
            "start": start,
            "end": end,
            "description": "Bring snacks.",
            "tz": "Europe/Berlin",
        },
    )
    _must("calendar create(b)", s, ev, ok=(201,))
    event_id = ev["id"]
    state["calendar_event_id"] = event_id
    # Sanity: the host's own row carries the explicit tz immediately,
    # before any federation has happened. If this fails the bug is in
    # the create path, not the wire.
    if ev.get("tz") != "Europe/Berlin":
        print(
            f"  calendar create(b): host row tz={ev.get('tz')!r} (expected Europe/Berlin)"
        )
    print(f"  b: calendar event {event_id} created (tz=Europe/Berlin)")

    # Let SPACE_CALENDAR_EVENT_CREATED fan out to a + c.
    time.sleep(4)

    # 3. Alpha + Carol RSVP "going" — RSVP federates back to Beta.
    for guest_label in ("a", "c"):
        guest = state["instances"][guest_label]
        s, _r = _request(
            f"http://127.0.0.1:{guest['port']}/api/calendars/events/{event_id}/rsvp",
            token=guest["token"],
            method="POST",
            body={"status": "going"},
        )
        _must(f"rsvp({guest_label})", s, _r)
        print(f"  {guest_label} RSVP'd 'going'")

    # Settle: SPACE_CALENDAR_RSVP envelopes round-trip to Beta.
    time.sleep(4)

    # 4. Beta sees both RSVPs.
    s, rsvps = _request(
        f"http://127.0.0.1:{b['port']}/api/calendars/events/{event_id}/rsvps",
        token=b["token"],
    )
    _must("rsvps(b)", s, rsvps)
    rows = rsvps.get("rsvps") or []
    going = {r["user_id"]: r["status"] for r in rows if r["status"] == "going"}
    expected = {
        state["instances"]["a"]["user_id"],
        state["instances"]["c"]["user_id"],
    }
    missing = expected - set(going.keys())
    if missing:
        raise SystemExit(
            f"b: missing RSVPs from {sorted(missing)}; got {rows!r}",
        )
    print(f"  b sees {sorted(going.keys())} going ✓")
    _save(state)
    print("calendar: ok")

    _save(state)
    print("traffic: ok")


# ─── Step: verify ──────────────────────────────────────────────────────────


def _all_display_names(state: dict, viewer_label: str) -> dict[str, str]:
    """Return ``{user_id: display_name}`` everyone the viewer can see."""
    info = state["instances"][viewer_label]
    s, fr = _request(
        f"http://127.0.0.1:{info['port']}/api/friends",
        token=info["token"],
    )
    _must(f"friends({viewer_label})", s, fr)
    out: dict[str, str] = {}
    for m in fr["instance"]["members"]:
        out[m["user_id"]] = m["display_name"]
    for h in fr["households"]:
        for m in h["members"]:
            out[m["user_id"]] = m["display_name"]
    return out


def _highlight_captions(state: dict, viewer_label: str) -> set[str]:
    info = state["instances"][viewer_label]
    s, hls = _request(
        f"http://127.0.0.1:{info['port']}/api/highlights",
        token=info["token"],
    )
    _must(f"highlights({viewer_label})", s, hls)
    return {f["caption_text"] or "" for h in hls for f in h["frames"]}


def cmd_verify() -> None:
    """Assert each household sees the others' federated content."""
    state = _load()
    if not state:
        raise SystemExit("run 'up' first")

    failures: list[str] = []

    # 1. Profile sync — every household sees the other two users by display name.
    for viewer in ("a", "b", "c"):
        names = _all_display_names(state, viewer)
        for other in ("a", "b", "c"):
            if other == viewer:
                continue
            other_user = state["instances"][other]["user_id"]
            other_uname = state["instances"][other]["username"]
            shown = names.get(other_user)
            if not shown or other_uname not in shown.lower():
                failures.append(
                    f"{viewer}: profile of {other} ({other_uname}) "
                    f"missing or wrong (got {shown!r})"
                )
            else:
                print(f"  {viewer} sees {other}: {shown}")

    # 2. Highlights — each household authored one with audience all_paired,
    #    so the other two should see it.
    for viewer in ("a", "b", "c"):
        captions = _highlight_captions(state, viewer)
        for other in ("a", "b", "c"):
            if other == viewer:
                continue
            needle = f"[{other}] highlight — audience all_paired"
            if needle not in captions:
                failures.append(
                    f"{viewer}: highlight from {other} not visible "
                    f"(captions={sorted(captions)})"
                )
            else:
                print(f"  {viewer} sees {other}'s highlight ✓")

    # 3. DM a→c — Carol's conversation list should include the new DM
    #    and the message body should round-trip.
    if "dm_a_to_c" in state:
        c = state["instances"]["c"]
        s, convs = _request(
            f"http://127.0.0.1:{c['port']}/api/conversations",
            token=c["token"],
        )
        _must("conversations(c)", s, convs)
        conv_list = convs if isinstance(convs, list) else (convs.get("items") or [])
        ids = {c0.get("id") for c0 in conv_list}
        if state["dm_a_to_c"] not in ids:
            failures.append(f"c: conversation {state['dm_a_to_c']} not in c's inbox")
        else:
            s, msgs = _request(
                f"http://127.0.0.1:{c['port']}"
                f"/api/conversations/{state['dm_a_to_c']}/messages",
                token=c["token"],
            )
            _must("messages(c)", s, msgs)
            bodies = [
                m.get("content") for m in (msgs if isinstance(msgs, list) else [])
            ]
            if any("hello carol from alice" in (b or "") for b in bodies):
                print(f"  c received a→c DM ({len(bodies)} msg) ✓")
            else:
                failures.append(f"c: a→c DM body missing (got {bodies!r})")

            # 3a. v_3 media DM a→c — assert Carol's view of the
            #     image message. The DM_MESSAGE envelope carries the
            #     preview (renders immediately as a 320 px WebP);
            #     the DM_MEDIA_BLOB follow-up flips media_url to the
            #     full file under media_dir and clears
            #     media_sync_status. We give the scheduler a short
            #     grace period (~8 s) before asserting the
            #     full-bytes state — its tick interval defaults to
            #     5 s.
            if "dm_media_msg_id" in state:
                # Wait for the scheduler to flush the blob outbox.
                time.sleep(8)
                msg_list = msgs if isinstance(msgs, list) else []
                # Re-fetch since the blob may have landed after the
                # preview's initial GET above.
                s2, msgs2 = _request(
                    f"http://127.0.0.1:{c['port']}"
                    f"/api/conversations/{state['dm_a_to_c']}/messages",
                    token=c["token"],
                )
                _must("media-messages(c)", s2, msgs2)
                msg_list = msgs2 if isinstance(msgs2, list) else []
                media_msg = next(
                    (m for m in msg_list if m.get("type") == "image"),
                    None,
                )
                if media_msg is None:
                    failures.append(
                        "c: media DM a→c not in inbox (no type=image row)",
                    )
                else:
                    media_url = media_msg.get("media_url") or ""
                    sync_status = media_msg.get("media_sync_status")
                    if sync_status is not None and sync_status != "":
                        # Still pending — the blob hasn't landed yet.
                        # Surface as a failure so we notice scheduler
                        # regressions; the 8 s wait should be more
                        # than enough for a healthy boot.
                        failures.append(
                            "c: media DM a→c still pending after wait "
                            f"(sync_status={sync_status!r}, media_url={media_url!r})",
                        )
                    elif not media_url:
                        failures.append(
                            "c: media DM a→c has no media_url after blob land",
                        )
                    else:
                        # HEAD the signed media URL on Carol's
                        # instance — confirms the file landed under
                        # ``media_dir`` and the signing chain works
                        # for the receiver-side read. ``_request``
                        # assumes JSON, so we hit urllib directly
                        # with a HEAD verb that doesn't return a
                        # body to decode.
                        full_url = media_url
                        if not full_url.startswith("http"):
                            full_url = (
                                f"http://127.0.0.1:{c['port']}/{full_url.lstrip('/')}"
                            )
                        head_req = urllib.request.Request(
                            full_url,
                            method="HEAD",
                            headers={"Authorization": f"Bearer {c['token']}"},
                        )
                        try:
                            with urllib.request.urlopen(head_req, timeout=10) as r:
                                ms = r.status
                        except urllib.error.HTTPError as exc:
                            ms = exc.code
                        if ms not in (200, 304):
                            failures.append(
                                f"c: media DM a→c file fetch HTTP {ms} ({media_url})",
                            )
                        else:
                            print(
                                "  c received a→c media DM (preview + "
                                "full bytes via DM_MEDIA_BLOB) ✓",
                            )
    else:
        print("  DM a→c was skipped during traffic step")

    # 3b. Bazaar DM a→b — Beta's conversation list should include
    #     Alpha's inquiry, with the listing id quoted in the body.
    if "dm_a_to_b" in state and "bazaar_listing_id" in state:
        b = state["instances"]["b"]
        s, convs = _request(
            f"http://127.0.0.1:{b['port']}/api/conversations",
            token=b["token"],
        )
        _must("conversations(b)", s, convs)
        conv_list = convs if isinstance(convs, list) else (convs.get("items") or [])
        ids = {c0.get("id") for c0 in conv_list}
        if state["dm_a_to_b"] not in ids:
            failures.append(
                f"b: bazaar-inquiry conversation {state['dm_a_to_b']} "
                f"not in Beta's inbox",
            )
        else:
            s, msgs = _request(
                f"http://127.0.0.1:{b['port']}"
                f"/api/conversations/{state['dm_a_to_b']}/messages",
                token=b["token"],
            )
            _must("messages(b/bazaar)", s, msgs)
            bodies = [
                m.get("content") for m in (msgs if isinstance(msgs, list) else [])
            ]
            needle = state["bazaar_listing_id"]
            if any(needle in (m_body or "") for m_body in bodies):
                print(f"  b received a→b bazaar-inquiry DM ✓")
            else:
                failures.append(
                    f"b: bazaar-inquiry DM body missing listing id "
                    f"{needle!r} (got {bodies!r})",
                )
    elif "bazaar_listing_id" not in state:
        print("  bazaar listing skipped during traffic step")
    else:
        print("  DM a→b was skipped during traffic step")

    # 3c. Momentum visibility — a + c follow b on momentum, so Beta's
    #     moment from cmd_traffic should land in their inbox after
    #     federation settles. Beta's own moment should be visible to
    #     Beta locally.
    beta_moment = state.get("moments", {}).get("b")
    if beta_moment and "moment_follows" in state:
        for viewer_label in ("a", "c"):
            if viewer_label not in state["moment_follows"]:
                continue
            viewer = state["instances"][viewer_label]
            s, payload = _request(
                f"http://127.0.0.1:{viewer['port']}/api/moments",
                token=viewer["token"],
            )
            _must(f"moments({viewer_label})", s, payload)
            inbox = payload.get("data") if isinstance(payload, dict) else payload
            inbox_list = inbox if isinstance(inbox, list) else []
            contents = [m.get("content") for m in inbox_list]
            if any(beta_moment["content"] in (mc or "") for mc in contents):
                print(f"  {viewer_label} sees b's moment in inbox ✓")
            else:
                failures.append(
                    f"{viewer_label}: b's moment {beta_moment['id']!r} "
                    f"not in inbox (got contents={contents!r})",
                )
    elif beta_moment is None:
        print("  Beta's moment was rate-limited during traffic — skipping inbox check")
    else:
        print("  moment-follow step was skipped during traffic")

    # 4. Space — Beta's space, both Alice and Carol invited.
    if "space_id" in state:
        b = state["instances"]["b"]
        s, members = _request(
            f"http://127.0.0.1:{b['port']}/api/spaces/{state['space_id']}/members",
            token=b["token"],
        )
        if s != 200:
            print(f"  space members: {s} {members}")
        else:
            mlist = members if isinstance(members, list) else members.get("members", [])
            ids = {m.get("user_id") for m in mlist}
            for label in ("a", "c"):
                uid = state["instances"][label]["user_id"]
                if uid in ids:
                    print(f"  space contains {label} ✓")
                else:
                    print(f"  space pending acceptance from {label}")

    # 5. Trust-relay pair — a ↔ d should be CONFIRMED on both sides
    #    *if* :func:`cmd_relay_pair` was run (excluded from ``all``).
    if "relay_pair_ran" in state:
        a_iid = state["instances"]["a"]["instance_id"]
        d_iid = state["instances"]["d"]["instance_id"]
        for viewer, peer in (("a", d_iid), ("d", a_iid)):
            info = state["instances"][viewer]
            s, conns = _request(
                f"http://127.0.0.1:{info['port']}/api/pairing/connections",
                token=info["token"],
            )
            _must(f"connections({viewer})", s, conns)
            match = [c for c in conns if c["instance_id"] == peer]
            if not match or match[0]["status"] != "confirmed":
                failures.append(
                    f"{viewer}: relay-paired peer {peer[:8]} missing or not confirmed",
                )
                continue
            print(f"  {viewer} ↔ {peer[:8]} confirmed via trust relay ✓")
            # Capability handshake must reach trust-relay-paired peers
            # too — the auto-pair coordinator publishes ``PairingConfirmed``
            # for both sides of the relay so the same on-pair announcement
            # subscriber fires. Without this assertion a regression that
            # only the *responder* fires the event (the same bug we hit
            # for QR pairs) would slip past the inner-ring check.
            pv = int(match[0].get("proto_version") or 1)
            if pv < 2:
                failures.append(
                    f"{viewer}: relay-paired peer {peer[:8]} stuck at "
                    f"proto_version={pv} (expected >= 2) — "
                    f"INSTANCE_CAPABILITIES_UPDATED never landed on the "
                    f"trust-relay path?",
                )
            else:
                print(
                    f"  {viewer} sees {peer[:8]} at proto_version={pv} "
                    f"(trust-relay) ✓",
                )
    else:
        print("  trust-relay pair (a ↔ d via b) skipped — run 'relay-pair' to exercise")

    # 6. Calendar RSVPs — Beta's space-calendar event has 'going' from
    #    both Alpha and Carol after federation. Only asserted when
    #    ``cmd_calendar`` ran; that step is excluded from ``all`` until
    #    SPACE_CALENDAR_EVENT_CREATED outbound federation is wired.
    if "calendar_event_id" in state:
        b = state["instances"]["b"]
        s, rsvps = _request(
            f"http://127.0.0.1:{b['port']}/api/calendars/events/"
            f"{state['calendar_event_id']}/rsvps",
            token=b["token"],
        )
        _must("rsvps(b)", s, rsvps)
        going = {
            r["user_id"] for r in (rsvps.get("rsvps") or []) if r["status"] == "going"
        }
        for label in ("a", "c"):
            uid = state["instances"][label]["user_id"]
            if uid in going:
                print(f"  b sees {label} 'going' ✓")
            else:
                failures.append(
                    f"b: RSVP from {label} ({uid}) missing (got {sorted(going)})",
                )

        # 6b. tz field round-trip — Beta authored the event with
        #     ``tz="Europe/Berlin"``. After SPACE_CALENDAR_EVENT_CREATED
        #     federates to Alpha and Carol, their local mirror of the
        #     event must carry the same tz. Asserts the v2 field
        #     actually rides through the wire (the proto_version check
        #     alone only proves the *announcement* propagated; this
        #     proves a v2 field on a federated event reaches the
        #     receivers in shape).
        #
        #     Uses the space-scoped events list rather than the per-
        #     event GET because §D1b cross-household remote-invitees
        #     don't pass the personal-calendar route's space-membership
        #     check — the event lives in ``space_calendar_events`` on
        #     the peer side, addressable only via the space endpoint.
        space_id = state["space_id"]
        evt_id = state["calendar_event_id"]
        # ``Z`` not ``+00:00`` — the URL decoder turns ``+`` into a
        # space, which then fails ``datetime.fromisoformat`` server-
        # side and surfaces as a 422 with a generic detail message.
        window_start = "2027-01-01T00:00:00Z"
        window_end = "2027-02-01T00:00:00Z"
        for guest_label in ("a", "c"):
            guest = state["instances"][guest_label]
            s, events = _request(
                f"http://127.0.0.1:{guest['port']}/api/spaces/{space_id}"
                f"/calendar/events?start={window_start}&end={window_end}",
                token=guest["token"],
            )
            _must(f"space calendar events({guest_label})", s, events)
            evt_list = events if isinstance(events, list) else (
                events.get("events") or []
            )
            mirror = next((e for e in evt_list if e.get("id") == evt_id), None)
            if mirror is None:
                failures.append(
                    f"{guest_label}: federated calendar event {evt_id} "
                    f"not visible in space-scoped list — "
                    f"SPACE_CALENDAR_EVENT_CREATED did not land",
                )
                continue
            tz = mirror.get("tz")
            if tz != "Europe/Berlin":
                failures.append(
                    f"{guest_label}: federated event tz={tz!r} "
                    f"(expected 'Europe/Berlin') — v2 tz field did not "
                    f"ride through SPACE_CALENDAR_EVENT_CREATED",
                )
            else:
                print(f"  {guest_label} sees event tz=Europe/Berlin ✓")

    # 7. Capability handshake — every confirmed inner-ring peer should
    #    have announced their proto_version via
    #    ``INSTANCE_CAPABILITIES_UPDATED`` at startup. After ``up`` + a
    #    short settle window we expect each of a/b/c to see the others
    #    at proto_version >= 2 (the version this build advertises). A
    #    peer still pinned at 1 means the announcement never landed —
    #    most likely the outbound didn't fire or the inbound handler is
    #    not registered. The harness asserts the round-trip so future
    #    additive-but-not-fail-soft features have a safety net.
    for viewer in ("a", "b", "c"):
        info = state["instances"][viewer]
        s, conns = _request(
            f"http://127.0.0.1:{info['port']}/api/pairing/connections",
            token=info["token"],
        )
        _must(f"connections({viewer})", s, conns)
        peers_by_id = {c["instance_id"]: c for c in conns}
        for other in ("a", "b", "c"):
            if other == viewer:
                continue
            other_iid = state["instances"][other]["instance_id"]
            row = peers_by_id.get(other_iid)
            if row is None:
                failures.append(
                    f"{viewer}: missing pairing connection row for {other}",
                )
                continue
            pv = int(row.get("proto_version") or 1)
            if pv < 2:
                failures.append(
                    f"{viewer}: peer {other} stuck at proto_version={pv} "
                    f"(expected >= 2) — INSTANCE_CAPABILITIES_UPDATED "
                    f"never landed?",
                )
            else:
                print(f"  {viewer} sees {other} at proto_version={pv} ✓")

    # 8. Crash check — every instance still alive (WebRTC didn't blow up).
    for label, info in state["instances"].items():
        if not _alive(info["pid"]):
            failures.append(f"{label}: process pid={info['pid']} is gone")

    # 9. Log audit — scan each backend's stdout/stderr for unhandled
    #    exceptions, ERROR-level lines, federation-pipeline rejects.
    #    Anything we can't account for (i.e. doesn't match the
    #    benign-noise allow-list) becomes a verify failure so the
    #    next run forces it to be either fixed or explicitly excused.
    failures.extend(_audit_logs(state))

    if failures:
        print("\n--- FAIL ---")
        for f in failures:
            print(f"  {f}")
        sys.exit(1)
    print("verify: ok")


# ─── Log audit — surface backend exceptions ────────────────────────────────


#: Substrings that mark a log line as known-benign noise we should NOT
#: flag in :func:`_audit_logs`. Each entry is a comment so the next
#: person to trip a new line knows whether to suppress or fix.
_LOG_BENIGN: tuple[str, ...] = (
    # Outbox retry warning while a peer is briefly unreachable —
    # expected during the inner-ring pair handshake settle window
    # (peer is booting / DNS/RPS not yet warm). Narrow to the
    # specific "returned HTTP" pattern so a *different* outbox
    # error (e.g. shutdown corruption, schema mismatch) still
    # surfaces.
    "outbox: ", " returned HTTP ",
    # libdatachannel native ICE state-machine status lines. The
    # harness intentionally configures STUN against an external
    # endpoint while the instances themselves talk loopback-only,
    # so the C library logs candidate-gathering failures /
    # connectivity-timer expiries that are pure environment noise.
    # Narrow to the specific juice/SCTP status messages so a
    # different libdatachannel-level error still surfaces.
    "juice: Changing state to",
    "juice: Connectivity timer",
    "juice: Got STUN mapped address",
    "juice: STUN server binding successful",
    "juice: Candidate gathering done",
    "juice: Using STUN server",
    # The rate-limit middleware's own audit log fires when
    # relay-pair waits the 65 s window; expected by design.
    "rate_limit",
    # Earlier entries removed because the underlying conditions
    # were fixed upstream (instead of permanently allowlisted):
    # * ``FederationEventType.FEDERATION_RTC_ICE`` /
    #   ``rtcAddRemoteCandidate: runtime failure`` /
    #   ``Got a remote candidate without remote description``
    #   were all symptoms of an offer-vs-candidate race in
    #   ``aiolibdatachannel`` < 2026.5.10. The 2026.5.10 buffer
    #   fix eliminates them entirely; if they reappear, the
    #   audit catches a regression.
    # * The previous ``rtc::impl::IceTransport::LogCallback`` /
    #   ``aiolibdatachannel:rtc::`` blanket-suppressions covered
    #   every C++-side log including *real* ERROR lines. Replaced
    #   with the specific juice/STUN status entries above so a
    #   genuine libdatachannel failure still trips the audit.
)

#: Substrings that — when they appear — are real signal worth
#: surfacing. These are the patterns an exception traceback or an
#: explicit ``log.error`` / ``log.warning`` produces.
_LOG_INTERESTING: tuple[str, ...] = (
    "Traceback (most recent call last):",
    "ERROR:",
    "WARNING:",
    "Exception:",
)


def _audit_logs(state: dict) -> list[str]:
    """Return a list of failure strings, one per offending log block.

    Each instance writes ``log.txt`` under its data dir; the GFS does
    the same under ``gfs/log.txt``. We split the file into "blocks"
    (each block is a single ERROR/WARNING/Traceback and the
    indented frames that follow it), and a block is suppressed if
    *any* line within it matches a substring in :data:`_LOG_BENIGN`.
    That way an RTC ICE traceback whose tail line names the
    allow-listed cause stays suppressed even though the leading
    ``Traceback`` line itself doesn't contain the benign needle.
    Reports up to 5 distinct offending blocks per file (anything more
    is usually the same root cause repeating)."""
    failures: list[str] = []
    sources = list((state.get("instances") or {}).items())
    if state.get("gfs"):
        sources.append(("gfs", {"log_path": str(GFS_DIR / "log.txt")}))
    for label, info in sources:
        path = Path(info.get("log_path") or _instance_dir(label) / "log.txt")
        if not path.exists():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        blocks = _split_log_into_blocks(text)
        hits: list[str] = []
        for header, block_text in blocks:
            if not any(marker in header for marker in _LOG_INTERESTING):
                continue
            if any(needle in block_text for needle in _LOG_BENIGN):
                continue
            hits.append(header)
            if len(hits) >= 5:
                break
        for h in hits:
            failures.append(f"{label}: log audit — {h.strip()[:200]}")
    return failures


def _split_log_into_blocks(text: str) -> list[tuple[str, str]]:
    """Group lines into ``(header, full_block)`` tuples.

    A block starts at the first non-indented line with one of
    :data:`_LOG_INTERESTING` markers and continues through every
    subsequent indented frame (``  File "...``,
    ``    cursor.execute(...)`` etc.) plus the final exception-type
    summary line. Blank lines reset the block. The header is what
    we match for "is this interesting"; the full block text is what
    the benign filter scans, so a Traceback whose final line names
    a known-benign cause gets suppressed cleanly.
    """
    out: list[tuple[str, str]] = []
    cur_header: str | None = None
    cur_lines: list[str] = []

    def _flush() -> None:
        nonlocal cur_header, cur_lines
        if cur_header is not None:
            out.append((cur_header, "\n".join(cur_lines)))
        cur_header = None
        cur_lines = []

    for line in text.splitlines():
        if not line.strip():
            _flush()
            continue
        # Continuation lines: leading whitespace, or the
        # ``ExceptionType: ...`` summary that closes a Traceback.
        is_continuation = (
            cur_header is not None
            and (line.startswith((" ", "\t")) or _looks_like_exc_summary(line))
        )
        if is_continuation:
            cur_lines.append(line)
            continue
        # Otherwise this line starts a new (header) block.
        _flush()
        cur_header = line
        cur_lines = [line]
    _flush()
    return out


def _looks_like_exc_summary(line: str) -> bool:
    """Heuristic: lines that look like ``ExceptionType: detail`` —
    closing summary of a traceback. We coalesce them onto the
    in-progress block so the benign filter can scan them.

    Accepts dotted forms (``mod.sub.RTCError: …``) as well as bare
    type names (``ValueError: …``); the rule is "no whitespace
    before the first colon and the rightmost dotted component starts
    with a capital".
    """
    stripped = line.strip()
    if ":" not in stripped:
        return False
    head = stripped.split(":", 1)[0]
    if not head or " " in head:
        return False
    last = head.rsplit(".", 1)[-1]
    return bool(last) and last[0].isupper()


# ─── Step: down ────────────────────────────────────────────────────────────


def cmd_visibility() -> None:
    """Per-pair user-visibility — Alpha hides a local user from Beta.

    Validates the outbound peer-user-visibility filter (§Connection
    Detail UX): Alpha provisions a second local user (``ada``), Beta's
    ``/api/friends`` confirms ada is mirrored locally, Alpha PATCHes
    ``/api/pairing/connections/{beta_id}/visible-users`` to hide ada,
    and we assert ada disappears from Beta's ``/api/friends`` after
    federation settles. Then we flip ada back to visible and confirm
    she reappears.

    Sequence:
    1. Provision a second user on Alpha via
       ``POST /api/admin/users {username, password, display_name}``.
    2. Patch the new user's profile so a ``USER_UPDATED`` envelope
       fans out to Beta — i.e. Beta now mirrors ``ada`` in
       ``remote_users``. Wait briefly for federation settle.
    3. Assert Beta's ``/api/friends`` includes Alpha's household and
       lists Ada among its members.
    4. Hide ada from Beta via the visibility PATCH; the route fans a
       ``USER_REMOVED`` to Beta.
    5. Wait, then assert Beta's ``/api/friends`` no longer lists ada
       (the rest of the household stays).
    6. Flip ada back to visible; the route fans a ``USER_UPDATED``.
    7. Wait, then assert Beta sees ada again.

    Prereq: ``up`` + ``pair`` (a ↔ b must be confirmed).
    """
    state = _load()
    if not state:
        raise SystemExit("run 'up' first")

    a = state["instances"]["a"]
    b = state["instances"]["b"]
    a_url = f"http://127.0.0.1:{a['port']}"
    b_url = f"http://127.0.0.1:{b['port']}"

    # ``cmd_pair`` burns through the /api/pairing rate-limit window
    # (mostly via the four QR handshakes + their settle). The visible-
    # users PATCH lives under the same ``/api/pairing/*`` bucket as
    # /api/pairing/initiate, so a quick run of ``up → pair →
    # visibility`` would 429 here. Drain the window the same way
    # ``cmd_relay_pair`` does.
    if state.get("rate_limit_drained_for") != "visibility":
        wait = 65
        print(f"  waiting {wait}s for /api/pairing/* rate-limit window to drain...")
        time.sleep(wait)
        state["rate_limit_drained_for"] = "visibility"
        _save(state)

    # 1. Provision a second user on Alpha.
    new_username = "ada"
    new_display = "Ada Lovelace"
    s, prov = _request(
        f"{a_url}/api/admin/users",
        token=a["token"],
        method="POST",
        body={
            "username": new_username,
            "password": "harness-pwd-ada",
            "display_name": new_display,
            "is_admin": False,
        },
    )
    # 409 = already exists from a prior run; reuse.
    if s == 409:
        s, listing = _request(
            f"{a_url}/api/users",
            token=a["token"],
        )
        _must("list users(a)", s, listing)
        ada = next(
            (u for u in listing if u.get("username") == new_username),
            None,
        )
        if ada is None:
            raise SystemExit("visibility: ada exists per 409 but not in /api/users")
        ada_user_id = ada["user_id"]
    else:
        _must("provision ada(a)", s, prov, ok=(201,))
        ada_user_id = prov["user_id"]
    state["visibility_user_id"] = ada_user_id
    print(f"  a: provisioned {new_username} ({ada_user_id[:8]}…)")

    # 2. Trigger a profile update so USER_UPDATED federates to Beta.
    #    ``UserProfileUpdated`` is published by ``user_service.update_profile``
    #    which only the user themselves can invoke (PATCH /api/me).
    #    Log in as ada to get a bearer token, then PATCH /api/me.
    s, login = _request(
        f"{a_url}/api/auth/token",
        method="POST",
        body={"username": new_username, "password": "harness-pwd-ada"},
    )
    _must("ada login(a)", s, login)
    ada_token = login["token"]
    s, _ = _request(
        f"{a_url}/api/me",
        token=ada_token,
        method="PATCH",
        body={"display_name": new_display, "bio": "Hello from Alpha (visibility test)"},
    )
    _must("ada profile patch(a)", s, _)
    time.sleep(3)

    # 3. Beta sees Ada in /api/friends.
    def _friends_users_for(viewer_url: str, viewer_token: str, owner_iid: str) -> set[str]:
        s, payload = _request(
            f"{viewer_url}/api/friends",
            token=viewer_token,
        )
        _must("friends(b)", s, payload)
        households = payload.get("households", []) or []
        for h in households:
            if h.get("instance_id") == owner_iid:
                return {m.get("user_id") for m in (h.get("members") or [])}
        return set()

    seen = _friends_users_for(b_url, b["token"], a["instance_id"])
    if ada_user_id not in seen:
        raise SystemExit(
            f"visibility precheck: ada {ada_user_id} not yet visible to Beta "
            f"(saw {sorted(seen)!r}) — federation may not have settled yet",
        )
    print(f"  pre-check: Beta sees ada via /api/friends ✓")

    # 4. Hide ada from Beta.
    s, body = _request(
        f"{a_url}/api/pairing/connections/{b['instance_id']}/visible-users",
        token=a["token"],
        method="PATCH",
        body={"updates": [{"user_id": ada_user_id, "visible": False}]},
    )
    _must("hide ada(a→b)", s, body)
    rows = {u["user_id"]: u for u in body.get("users", [])}
    if rows.get(ada_user_id, {}).get("visible") is not False:
        raise SystemExit(
            f"visibility: hide PATCH did not flip ada to hidden — got {body!r}",
        )
    print(f"  a hid ada from Beta — USER_REMOVED fan-out queued")

    # 5. Wait for USER_REMOVED to land and Beta's mirror to drop ada.
    time.sleep(4)
    seen_after_hide = _friends_users_for(b_url, b["token"], a["instance_id"])
    if ada_user_id in seen_after_hide:
        raise SystemExit(
            f"visibility: Beta still sees ada {ada_user_id} after hide — "
            f"saw {sorted(seen_after_hide)!r}",
        )
    print(f"  post-hide: Beta no longer lists ada ✓")

    # 6. Flip back to visible — Alpha's PATCH sends USER_UPDATED.
    s, body = _request(
        f"{a_url}/api/pairing/connections/{b['instance_id']}/visible-users",
        token=a["token"],
        method="PATCH",
        body={"updates": [{"user_id": ada_user_id, "visible": True}]},
    )
    _must("unhide ada(a→b)", s, body)
    print(f"  a re-exposed ada to Beta — USER_UPDATED fan-out queued")

    # 7. Wait for USER_UPDATED to land and Beta to repopulate the row.
    #    Outbox redelivery + USER_UPDATED inbound + remote_users insert
    #    is several federation-pipeline hops; allow generous settle.
    deadline = time.monotonic() + 30.0
    seen_after_show: set[str] = set()
    while time.monotonic() < deadline:
        seen_after_show = _friends_users_for(b_url, b["token"], a["instance_id"])
        if ada_user_id in seen_after_show:
            break
        time.sleep(2)
    if ada_user_id not in seen_after_show:
        raise SystemExit(
            f"visibility: Beta still doesn't see ada after un-hide within 30s — "
            f"saw {sorted(seen_after_show)!r}",
        )
    print(f"  post-unhide: Beta sees ada again ✓")

    state["visibility_ran"] = True
    _save(state)
    print("visibility: ok (per-pair user-visibility filter works both ways)")


def cmd_replay() -> None:
    """Federation outbox redelivery — kill Carol, post from Alpha, restart.

    Validates the §24 ResilientFederationOutbox path: when a paired
    peer is unreachable, the sender's outbox marks the entry pending
    and retries on a backoff. Once the peer is back up its
    ``/api/instance/config`` becomes reachable and the next outbox tick
    flushes the queued envelopes.

    Sequence:
    1. SIGTERM Carol's process; wait for exit.
    2. Alpha creates a new ``audience_kind=all_paired`` highlight with
       a unique caption — the harness later asserts Carol receives
       this exact caption (i.e. it didn't pre-exist from
       :func:`cmd_traffic`).
    3. Settle ~4 s so Alpha's outbox makes (and fails) one delivery
       attempt against the now-dead Carol — the entry transitions
       to ``unreachable`` status.
    4. Respawn Carol on the same port; wait for ``/api/instance/config``
       to answer 200.
    5. Settle the outbox redelivery window (default 30s exponential
       backoff; the harness sleeps 25 s which crosses the second
       backoff slot).
    6. Assert Carol's ``/api/highlights`` now contains the new caption.

    Run this *after* :func:`cmd_pair` so the a↔c link is confirmed
    (``cmd_traffic`` is optional — the test only depends on the
    pair). Re-running it twice in a single ``up`` is fine; the
    caption uses :func:`time.time_ns` so each run picks a unique
    needle.
    """
    state = _load()
    if not state:
        raise SystemExit("run 'up' first")

    a = state["instances"]["a"]
    c = state["instances"]["c"]
    needle_marker = f"replay-{time.time_ns()}"
    caption = f"[a] resilient highlight {needle_marker}"

    # 1. Tear down Carol — SIGTERM the process group so libdatachannel's
    #    background threads also exit cleanly.
    print(f"  killing c (pid={c['pid']}) to simulate offline peer")
    try:
        os.killpg(c["pid"], signal.SIGTERM)
    except ProcessLookupError:
        print("  c was already gone")
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline and _alive(c["pid"]):
        time.sleep(0.2)
    if _alive(c["pid"]):
        try:
            os.killpg(c["pid"], signal.SIGKILL)
        except ProcessLookupError:
            pass
        time.sleep(0.5)

    # 2. Alpha posts a highlight while Carol is dead. Alpha's outbox
    #    will queue the envelope; the next federation flush against
    #    Carol fails fast.
    s, _hl = _request(
        f"http://127.0.0.1:{a['port']}/api/highlights/frames",
        token=a["token"],
        method="POST",
        body={
            "media_url": "https://example.invalid/replay.jpg",
            "frame_type": "image",
            "caption_text": caption,
            "audience_kind": "all_paired",
        },
    )
    _must("replay highlight(a)", s, _hl, ok=(201,))
    print(f"  a created highlight while c offline (caption={needle_marker})")

    # 3. Let Alpha's outbox attempt + fail one delivery so the row is
    #    in the redeliver-pending state.
    time.sleep(4)

    # 4. Respawn Carol on the same port; reuse the existing per-instance
    #    data_dir so identity + paired peers stay intact.
    new_pid = _spawn("c", c["port"])
    state["instances"]["c"]["pid"] = new_pid
    _wait_ready(c["port"])
    print(f"  c respawned: pid={new_pid} ready=200")

    # 5. Outbox redelivery window. The default backoff schedule is
    #    {0, 5, 30, 120, 600}s; we already burned the immediate slot
    #    in step 3, so we sleep across the 30s slot to give the
    #    second attempt a chance to land.
    settle = 35
    print(f"  waiting {settle}s for outbox redelivery to flush…")
    time.sleep(settle)

    # 6. Carol should now have Alpha's highlight despite having been
    #    down at the moment Alpha posted it.
    captions = _highlight_captions(state, "c")
    if caption in captions:
        print(f"  c received the replayed highlight ✓")
    else:
        raise SystemExit(
            f"replay: Carol did not receive {caption!r} after redelivery "
            f"window — captions seen: {sorted(captions)!r}",
        )

    state["replay_ran"] = True
    _save(state)
    print("replay: ok")


def cmd_down() -> None:
    state = _load()
    gfs = state.get("gfs")
    if gfs:
        try:
            os.killpg(gfs["pid"], signal.SIGTERM)
            print(f"  gfs: SIGTERM pid={gfs['pid']}")
        except (ProcessLookupError, PermissionError):
            pass
    for label, info in (state.get("instances") or {}).items():
        try:
            os.killpg(info["pid"], signal.SIGTERM)
            print(f"  {label}: SIGTERM pid={info['pid']}")
        except (ProcessLookupError, PermissionError):
            pass
    time.sleep(1)
    if gfs:
        try:
            os.killpg(gfs["pid"], signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    for label, info in (state.get("instances") or {}).items():
        try:
            os.killpg(info["pid"], signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    if ROOT.exists():
        shutil.rmtree(ROOT)
    print("down: ok")


# ─── Entry point ───────────────────────────────────────────────────────────


def main() -> None:
    if len(sys.argv) != 2:
        print(__doc__, file=sys.stderr)
        sys.exit(2)
    cmd = sys.argv[1]
    if cmd == "all":
        # The full path: pair the inner ring → traffic + invites →
        # accept invites + RSVP-on-event (cmd_calendar) → assertions →
        # transitive auto-pair via the trust relay (cmd_relay_pair).
        # GFS lifecycle (``gfs-up`` / ``gfs-pair`` / ``gfs-down``) is
        # opt-in and not part of the canonical smoke run; the GFS
        # process is heavyweight to spin up and not strictly required
        # for the HFS↔HFS federation surface this skill validates.
        cmd_up()
        cmd_pair()
        cmd_traffic()
        time.sleep(5)  # let federation settle before assertions
        cmd_calendar()
        cmd_verify()
        cmd_relay_pair()
        # ``visibility`` toggles a local user hidden from a peer and
        # asserts the peer's ``/api/friends`` mirror tracks the
        # change. Validates the outbound peer-user-visibility filter.
        cmd_visibility()
        # ``replay`` exercises the §24 outbox redelivery path by
        # killing Carol, posting a highlight from Alpha, restarting
        # Carol, and asserting the queued envelope flushes after the
        # backoff window. Runs last so the kill-restart cycle can't
        # destabilise the earlier topology assertions.
        cmd_replay()
        return
    fn = globals().get(f"cmd_{cmd.replace('-', '_')}")
    if fn is None:
        raise SystemExit(f"unknown subcommand: {cmd!r}")
    fn()


if __name__ == "__main__":
    main()
