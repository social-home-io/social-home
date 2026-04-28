"""One-shot local-test bootstrap (standalone-mode dev helper).

Seeds the minimum rows for a logged-in admin so the SPA can be poked
without going through pairing. Defaults to ``/tmp/sh-local-data`` for
the data dir so you can ``rm -rf`` it without touching anything you
care about.

* ``platform_users`` row so ``POST /api/auth/token`` accepts the login
  (username "admin", password from ``$SH_BOOTSTRAP_PASSWORD`` or
  "admin" by default).
* Matching ``users`` row + a fresh ``api_tokens`` row so subsequent
  bearer-auth'd requests authenticate.

The standalone adapter doesn't auto-provision a first user on boot, so
this script fills the gap for local testing only. Run from the repo
root::

    python scripts/bootstrap_local.py

then start the server::

    SH_DATA_DIR=/tmp/sh-local-data python -m socialhome
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import secrets
import sys
from pathlib import Path

# Make the ``socialhome`` package importable when this file is run from
# the repo root as a plain script (not via ``python -m``).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from socialhome.config import Config  # noqa: E402
from socialhome.db.database import AsyncDatabase  # noqa: E402
from socialhome.platform.standalone.adapter import StandaloneAdapter  # noqa: E402


USERNAME = "admin"
DISPLAY_NAME = "Admin"
DEFAULT_DATA_DIR = "/tmp/sh-local-data"


async def main() -> None:
    # Default to /tmp so the local DB never lands next to user data.
    # Honours SH_DATA_DIR if set, but flips the global default before
    # Config reads env so the same dir is used by both this script and
    # the server invocation that follows.
    os.environ.setdefault("SH_DATA_DIR", DEFAULT_DATA_DIR)
    cfg = Config.from_env()
    Path(cfg.data_dir).mkdir(parents=True, exist_ok=True)
    db = AsyncDatabase(Path(cfg.db_path), batch_timeout_ms=10)
    await db.startup()

    password = os.environ.get("SH_BOOTSTRAP_PASSWORD", "admin")

    # platform_users — the password store /api/auth/token verifies against.
    pw_hash = StandaloneAdapter.hash_password(password)
    await db.enqueue(
        """
        INSERT INTO platform_users(username, display_name, is_admin, password_hash)
        VALUES(?, ?, 1, ?)
        ON CONFLICT(username) DO UPDATE SET
            password_hash=excluded.password_hash,
            is_admin=1
        """,
        (USERNAME, DISPLAY_NAME, pw_hash),
    )

    # users — the row the bearer middleware joins to via api_tokens.
    user_id = "uid-admin"
    await db.enqueue(
        """
        INSERT INTO users(username, user_id, display_name, is_admin)
        VALUES(?, ?, ?, 1)
        ON CONFLICT(username) DO UPDATE SET is_admin=1
        """,
        (USERNAME, user_id, DISPLAY_NAME),
    )

    # api_tokens — rotate on every bootstrap so the token printed below
    # is always valid, even if you re-run the script.
    raw = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    token_id = secrets.token_urlsafe(16)
    await db.enqueue(
        """
        INSERT INTO api_tokens(token_id, user_id, label, token_hash)
        VALUES(?, ?, 'bootstrap', ?)
        """,
        (token_id, user_id, token_hash),
    )

    await db.shutdown()

    print("─" * 60)
    print("Local-test bootstrap complete.")
    print(f"  data_dir    : {cfg.data_dir}")
    print(f"  db_path     : {cfg.db_path}")
    print(f"  username    : {USERNAME}")
    print(f"  password    : {password}")
    print(f"  bearer token: {raw}")
    print()
    print("Start the server next:")
    print(f"  SH_DATA_DIR={cfg.data_dir} python -m socialhome")
    print()
    print("Then start the vite dev server (proxies /api → :8099):")
    print("  cd client && pnpm exec vite --host 0.0.0.0")
    print()
    print("Open  http://localhost:5173/  in a browser, then in the console:")
    print(f"  localStorage.setItem('sh_token', {raw!r}); location.reload()")
    print()
    print("(The standalone /api/auth/token login flow currently mints into")
    print("platform_tokens which the bearer middleware doesn't query —")
    print("the bootstrap-issued token above goes through api_tokens, which")
    print("works. Fix is a separate PR.)")
    print("─" * 60)


if __name__ == "__main__":
    asyncio.run(main())
