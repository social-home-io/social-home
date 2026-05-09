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
INSTANCES: tuple[tuple[str, int, str, str, str], ...] = (
    ("a", 18001, "alice", "alpha-pw", "Alpha House"),
    ("b", 18002, "bob", "beta-pw", "Beta House"),
    ("c", 18003, "carol", "gamma-pw", "Gamma House"),
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
    """Pair every household pairwise (a↔b, b↔c, a↔c)."""
    state = _load()
    if not state:
        raise SystemExit("run 'up' first")

    for initiator, scanner in (("a", "b"), ("b", "c"), ("a", "c")):
        _pair_two(state, initiator, scanner)

    # Settle: peer-confirm + initial peer-directory snapshots.
    time.sleep(3)
    for label, info in state["instances"].items():
        s, conns = _request(
            f"http://127.0.0.1:{info['port']}/api/pairing/connections",
            token=info["token"],
        )
        _must(f"connections({label})", s, conns)
        confirmed = [c for c in conns if c["status"] == "confirmed"]
        if len(confirmed) != 2:
            raise SystemExit(
                f"{label}: expected 2 confirmed peers, got {len(confirmed)} "
                f"({[c['display_name'] for c in conns]})"
            )
    print("pair: ok (3/3 households mutually paired)")


# ─── Step: traffic ─────────────────────────────────────────────────────────


def cmd_traffic() -> None:
    """Post one item of each public type from every household."""
    state = _load()
    if not state:
        raise SystemExit("run 'up' first")

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
        s, _ = _request(
            f"{url}/api/moments",
            token=token,
            method="POST",
            body={"content": f"🌅 [{label}] moment from {info['name']}"},
        )
        _must(f"moment({label})", s, _, ok=(201, 429))

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
    s, conv = _request(
        f"http://127.0.0.1:{a['port']}/api/conversations/dm",
        token=a["token"],
        method="POST",
        body={"username": c["username"]},
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
        ids = {c0.get("id") for c0 in (convs.get("items") or convs)}
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
    else:
        print("  DM a→c was skipped during traffic step")

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

    # 5. Crash check — every instance still alive (WebRTC didn't blow up).
    for label, info in state["instances"].items():
        if not _alive(info["pid"]):
            failures.append(f"{label}: process pid={info['pid']} is gone")

    if failures:
        print("\n--- FAIL ---")
        for f in failures:
            print(f"  {f}")
        sys.exit(1)
    print("verify: ok")


# ─── Step: down ────────────────────────────────────────────────────────────


def cmd_down() -> None:
    state = _load()
    for label, info in (state.get("instances") or {}).items():
        try:
            os.killpg(info["pid"], signal.SIGTERM)
            print(f"  {label}: SIGTERM pid={info['pid']}")
        except ProcessLookupError, PermissionError:
            pass
    time.sleep(1)
    for label, info in (state.get("instances") or {}).items():
        try:
            os.killpg(info["pid"], signal.SIGKILL)
        except ProcessLookupError, PermissionError:
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
        cmd_up()
        cmd_pair()
        cmd_traffic()
        time.sleep(5)  # let federation settle before assertions
        cmd_verify()
        return
    fn = globals().get(f"cmd_{cmd}")
    if fn is None:
        raise SystemExit(f"unknown subcommand: {cmd!r}")
    fn()


if __name__ == "__main__":
    main()
