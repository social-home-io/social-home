---
name: chrome-devtools-setup
description: Troubleshoot the chrome-devtools-mcp plugin in this Debian-trixie devcontainer. The devcontainer bakes in Google Chrome (at ``/opt/google/chrome/chrome``, where the plugin hard-codes it), a wrapper that adds ``--no-sandbox`` + ``--disable-dev-shm-usage``, and Xvfb on ``DISPLAY=:99`` so the headful browser has an X server. Once those four pieces line up, ``mcp__plugin_chrome-devtools-mcp_chrome-devtools__*`` calls work end-to-end. Use this skill when (a) a chrome-devtools-mcp call still fails after a fresh devcontainer build with "Could not find Google Chrome" / "Missing X server" / "Operation not permitted" / immediately closes the page; (b) you need a screenshot from a local web service via the headless chromium fallback.

## How the bootstrap is wired now

The four prerequisites are part of ``.devcontainer/devcontainer.json`` —
nothing to run by hand on a fresh container:

1. ``ghcr.io/devcontainers-extra/features/google-chrome:1`` installs the
   official Google Chrome ``.deb``, landing the binary at
   ``/opt/google/chrome/chrome`` (the path the plugin hard-codes).
2. ``ghcr.io/devcontainers-extra/features/apt-packages:1`` with
   ``packages: "xvfb"`` adds ``/usr/bin/Xvfb`` and ``xvfb-run``.
3. ``postCreateCommand`` ``dpkg-divert``s the real binary to
   ``/opt/google/chrome/chrome.real`` and installs
   ``.devcontainer/chrome-wrapper.sh`` in its place. The wrapper adds
   ``--no-sandbox`` (the container can't ``CLONE_NEWUSER``) and
   ``--disable-dev-shm-usage`` (``/dev/shm`` is tiny in containers).
4. ``postStartCommand`` starts ``Xvfb :99`` if it isn't already
   running, and ``containerEnv`` exports ``DISPLAY=:99`` for every
   process — including the out-of-process MCP plugin.

So the canonical state after a clean rebuild is:

- ``/opt/google/chrome/chrome`` → wrapper (shell script).
- ``/opt/google/chrome/chrome.real`` → diverted Google Chrome binary.
- ``Xvfb`` running on ``:99``.
- ``DISPLAY=:99`` in the container environment.

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

- ``ls -l /opt/google/chrome/chrome /opt/google/chrome/chrome.real``
  → both must exist; the first should be a small shell script
  (``cat`` it), the second the real binary.
- ``pgrep -a Xvfb`` → must show one ``Xvfb :99 ...`` line. If empty,
  ``Xvfb :99 -screen 0 1280x900x24 -nolisten tcp >/tmp/xvfb.log 2>&1 &``
  starts it; check ``/tmp/xvfb.log`` if it dies.
- ``echo "$DISPLAY"`` → must be ``:99``. If not, the MCP plugin
  inherits whatever ``DISPLAY`` was at devcontainer create time —
  rebuilding the container picks up ``containerEnv``.
- ``DISPLAY=:99 /opt/google/chrome/chrome --version`` → must print a
  Chrome version. ``Operation not permitted`` means the wrapper at
  ``/opt/google/chrome/chrome`` isn't being used (maybe an
  ``apt-get upgrade`` overwrote it — re-run the ``dpkg-divert`` +
  ``install`` recipe from ``.devcontainer/devcontainer.json``'s
  ``postCreateCommand``).
- ``Could not find Google Chrome`` from the plugin → the divert is
  intact but the wrapper got deleted. Reinstall it:
  ``sudo install -m 0755 .devcontainer/chrome-wrapper.sh /opt/google/chrome/chrome``.
- ``Target closed`` immediately after ``new_page`` → the browser
  crashed. Re-run ``DISPLAY=:99 /opt/google/chrome/chrome --version``
  to surface the underlying error.

## Direct fallback — chromium without the plugin

When the plugin still misbehaves (or when you only need a screenshot,
not interactive control), call Chrome directly in headless mode. This
works even when ``Xvfb`` is down, because it doesn't need a display:

```bash
/opt/google/chrome/chrome \
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
infrastructure now lives in ``.devcontainer/`` (``devcontainer.json``
+ ``chrome-wrapper.sh``). Manual recovery only touches
``/opt/google/chrome/`` and ``/tmp/.X11-unix/`` and never anything
inside the project tree.
