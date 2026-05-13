---
name: chrome-devtools-setup
description: Troubleshoot the chrome-devtools-mcp plugin in this Debian-trixie devcontainer. The devcontainer bakes in Debian's ``chromium`` (via the ``chromium-driver`` apt package, landing the launcher at ``/usr/bin/chromium``), the ``--no-sandbox`` + ``--disable-dev-shm-usage`` flags via ``/etc/chromium.d/devcontainer.conf``, ``PUPPETEER_EXECUTABLE_PATH=/usr/bin/chromium`` so chrome-devtools-mcp / Puppeteer skip their bundled-Chrome download, and Xvfb on ``DISPLAY=:99`` so the headful browser has an X server. Once those four pieces line up, ``mcp__plugin_chrome-devtools-mcp_chrome-devtools__*`` calls work end-to-end. Use this skill when (a) a chrome-devtools-mcp call still fails after a fresh devcontainer build with "Could not find Chrome" / "Missing X server" / "Operation not permitted" / immediately closes the page; (b) you need a screenshot from a local web service via the headless chromium fallback.

## How the bootstrap is wired now

The four prerequisites are part of ``.devcontainer/devcontainer.json`` —
nothing to run by hand on a fresh container:

1. ``ghcr.io/devcontainers-extra/features/apt-packages:1`` with
   ``packages: "xvfb chromium-driver"`` installs Debian's
   ``chromium`` (pulled in by ``chromium-driver``), landing the
   launcher at ``/usr/bin/chromium``, plus ``/usr/bin/Xvfb`` and
   ``xvfb-run``.
2. ``postCreateCommand`` drops
   ``/etc/chromium.d/devcontainer.conf`` containing
   ``CHROMIUM_FLAGS="$CHROMIUM_FLAGS --no-sandbox
   --disable-dev-shm-usage"``. Debian's ``/usr/bin/chromium``
   launcher sources every ``/etc/chromium.d/*.conf`` and appends
   ``$CHROMIUM_FLAGS`` to argv, so every Chromium launch — including
   the one chrome-devtools-mcp / Puppeteer spawn — picks the flags
   up. ``--no-sandbox`` because the container can't
   ``CLONE_NEWUSER``; ``--disable-dev-shm-usage`` because
   ``/dev/shm`` is tiny in containers.
3. ``postStartCommand`` starts ``Xvfb :99`` if it isn't already
   running, and ``containerEnv`` exports ``DISPLAY=:99`` for every
   process — including the out-of-process MCP plugin.
4. ``containerEnv`` exports ``PUPPETEER_EXECUTABLE_PATH=/usr/bin/chromium``
   (and ``CHROME_PATH`` as the historical alias) so chrome-devtools-mcp
   uses the in-image Chromium instead of trying to download its own.

So the canonical state after a clean rebuild is:

- ``/usr/bin/chromium`` → Debian launcher (shell script) that picks up
  ``/etc/chromium.d/devcontainer.conf``.
- ``/etc/chromium.d/devcontainer.conf`` → one-line ``CHROMIUM_FLAGS``
  export.
- ``Xvfb`` running on ``:99``.
- ``DISPLAY=:99`` and ``PUPPETEER_EXECUTABLE_PATH=/usr/bin/chromium``
  in the container environment.

## Verifying the plugin works

```
mcp__plugin_chrome-devtools-mcp_chrome-devtools__new_page url=http://127.0.0.1:8099/
mcp__plugin_chrome-devtools-mcp_chrome-devtools__take_snapshot
mcp__plugin_chrome-devtools-mcp_chrome-devtools__take_screenshot filePath=/tmp/page.png
```

If those work, you're done. The plugin manages the browser lifecycle
itself — you don't need to keep a chrome process around between calls.

## Troubleshooting

If something is still wrong after a fresh container build, check each
piece in isolation:

- ``ls -l /usr/bin/chromium /etc/chromium.d/devcontainer.conf``
  → both must exist; ``cat`` the second to confirm it sets
  ``CHROMIUM_FLAGS`` with ``--no-sandbox --disable-dev-shm-usage``.
- ``pgrep -a Xvfb`` → must show one ``Xvfb :99 ...`` line. If empty,
  ``Xvfb :99 -screen 0 1280x900x24 -nolisten tcp >/tmp/xvfb.log 2>&1 &``
  starts it; check ``/tmp/xvfb.log`` if it dies.
- ``echo "$DISPLAY"`` → must be ``:99``; ``echo
  "$PUPPETEER_EXECUTABLE_PATH"`` → must be ``/usr/bin/chromium``. If
  either is missing, the MCP plugin inherits whatever env was at
  devcontainer create time — rebuilding the container picks up
  ``containerEnv``.
- ``DISPLAY=:99 /usr/bin/chromium --version`` → must print a
  Chromium version. ``Operation not permitted`` means the flags
  config isn't being sourced — re-run the ``tee
  /etc/chromium.d/devcontainer.conf`` recipe from
  ``.devcontainer/devcontainer.json``'s ``postCreateCommand``.
- ``Could not find Chrome`` from the plugin → Puppeteer didn't see
  ``PUPPETEER_EXECUTABLE_PATH``. Confirm the env is exported in the
  process tree that launched the MCP plugin (``cat
  /proc/$(pgrep -f chrome-devtools-mcp)/environ | tr '\0' '\n' | grep
  -i chrome``) and rebuild the container if not.
- ``Target closed`` immediately after ``new_page`` → the browser
  crashed. Re-run ``DISPLAY=:99 /usr/bin/chromium --version``
  to surface the underlying error.

## Direct fallback — chromium without the plugin

When the plugin still misbehaves (or when you only need a screenshot,
not interactive control), call Chromium directly in headless mode. This
works even when ``Xvfb`` is down, because it doesn't need a display:

```bash
/usr/bin/chromium \
  --headless --no-sandbox --disable-gpu --hide-scrollbars \
  --window-size=1280,2400 \
  --screenshot=/tmp/page.png \
  http://127.0.0.1:8099/
```

For static HTML inspection (no JS) plain ``curl`` is faster:

```bash
curl -s http://127.0.0.1:8099/ -o /tmp/page.html
```

## Files this skill touches

This skill is read-only when the bootstrap works correctly — the
infrastructure now lives in ``.devcontainer/devcontainer.json``.
Manual recovery only touches ``/etc/chromium.d/`` and
``/tmp/.X11-unix/`` and never anything inside the project tree.
