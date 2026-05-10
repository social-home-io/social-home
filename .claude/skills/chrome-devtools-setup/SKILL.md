---
name: chrome-devtools-setup
description: Bootstrap the chrome-devtools-mcp plugin (and its underlying browser) in this Debian-trixie devcontainer. The plugin expects ``/opt/google/chrome/chrome``; the container ships ``/usr/bin/chromium``; chromium needs ``--no-sandbox`` because the unprivileged user-namespace clone path isn't enabled here; and the plugin defaults to a headful launch so it also needs an X server (Xvfb) on ``$DISPLAY``. Once these four pieces line up, ``mcp__plugin_chrome-devtools-mcp_chrome-devtools__*`` calls work end-to-end. Use this skill when (a) a chrome-devtools-mcp call fails with "Could not find Google Chrome" / "Missing X server" / "Operation not permitted" / immediately closes the page; (b) the devcontainer has just been rebuilt; (c) you want to take a screenshot or interact with a local web service from inside the agent.
---

## When to invoke this skill

Run this skill the first time chrome-devtools-mcp errors out on a
fresh devcontainer, or whenever you see one of these symptoms:

- ``Could not find Google Chrome executable for channel 'stable' at /opt/google/chrome/chrome.``
- ``Missing X server to start the headful browser.``
- ``Failed to move to new namespace: PID namespaces supported, Network namespace supported, but failed: errno = Operation not permitted``
- ``Protocol error (Target.setDiscoverTargets): Target closed`` (browser launches and immediately exits)

Once the setup steps below succeed, ``new_page`` /
``take_screenshot`` / ``evaluate_script`` calls will work and
you don't need this skill again until the container is rebuilt.

## What's wrong out-of-the-box

The chrome-devtools-mcp plugin (npm package) was written for
desktop developers running real Chrome. In this devcontainer:

1. **Wrong binary path.** The plugin hard-codes
   ``/opt/google/chrome/chrome``. We have ``/usr/bin/chromium``.
2. **Sandbox can't initialise.** Chromium's setuid sandbox
   tries to ``CLONE_NEWUSER`` and fails — Debian's container
   images don't enable unprivileged userns. The browser exits
   immediately on first launch.
3. **Headful by default.** The plugin asks for a headful
   browser; the container has no Xorg + no ``$DISPLAY``.
4. **D-Bus warnings (cosmetic).** Chromium logs ``Failed to
   connect to socket /run/dbus/system_bus_socket`` — harmless,
   it falls through, but it clutters the output.

The setup below resolves (1)–(3) once and for all. (4) we just
ignore.

## Setup steps

You have ``sudo`` available — the container's ``vscode`` user is
in the sudoers file. Each step is idempotent.

### 1. Install Xvfb (virtual X server)

```bash
sudo apt-get install -y xvfb
```

This adds ``/usr/bin/Xvfb`` plus the ``xvfb-run`` wrapper.

### 2. Symlink chromium → chrome with a launch wrapper

The plugin expects ``/opt/google/chrome/chrome``. We can't change
that — but we can give it a wrapper that adds ``--no-sandbox``
(plus a couple of container-friendly flags):

```bash
sudo mkdir -p /opt/google/chrome
sudo tee /opt/google/chrome/chrome > /dev/null <<'EOF'
#!/bin/bash
exec /usr/bin/chromium \
    --no-sandbox \
    --disable-dev-shm-usage \
    "$@"
EOF
sudo chmod +x /opt/google/chrome/chrome
```

``--no-sandbox`` is safe in this devcontainer — we trust the
pages we're inspecting (our own local services). ``/dev/shm``
is small in containers, so we route shared memory to ``/tmp``
to avoid OOM-style crashes when chrome opens a heavy page.

### 3. Start Xvfb on ``:99`` (foreground process; one-time / per session)

```bash
sudo mkdir -p /tmp/.X11-unix
sudo chmod 1777 /tmp/.X11-unix
Xvfb :99 -screen 0 1280x900x24 -nolisten tcp &
export DISPLAY=:99
```

Add ``DISPLAY=:99`` to ``~/.bashrc`` if you want it to stick across
new shells:

```bash
echo 'export DISPLAY=:99' >> ~/.bashrc
```

### 4. Sanity-check the chrome binary

```bash
DISPLAY=:99 /opt/google/chrome/chrome --version
```

Should print something like ``Chromium 148.0.x``. If you get a
sandbox error here, the wrapper at ``/opt/google/chrome/chrome``
isn't being used — re-run step 2.

## Calling the plugin

Once the four prereqs are in place, the plugin tools work:

```
mcp__plugin_chrome-devtools-mcp_chrome-devtools__new_page url=http://127.0.0.1:18765/
mcp__plugin_chrome-devtools-mcp_chrome-devtools__take_snapshot
mcp__plugin_chrome-devtools-mcp_chrome-devtools__take_screenshot filePath=/tmp/page.png
mcp__plugin_chrome-devtools-mcp_chrome-devtools__list_pages
```

The plugin auto-attaches to the chromium process spawned via the
wrapper at ``/opt/google/chrome/chrome``; you don't need to keep
the chrome process around between tool calls — the plugin manages
the browser lifecycle.

## Direct fallback — chromium without the plugin

When the plugin still misbehaves (or when you only need a
screenshot, not interactive control), call chromium directly. This
works regardless of whether steps 2–4 above succeeded, since it
doesn't need a display:

```bash
/usr/bin/chromium \
  --headless --no-sandbox --disable-gpu --hide-scrollbars \
  --window-size=1280,2400 \
  --screenshot=/tmp/page.png \
  http://127.0.0.1:18765/
```

For static HTML inspection (no JS) plain ``curl`` is faster:

```bash
curl -s http://127.0.0.1:18765/ -o /tmp/page.html
```

## Troubleshooting

- ``Operation not permitted`` from chrome → the wrapper at
  ``/opt/google/chrome/chrome`` either doesn't exist or isn't
  executable. ``ls -l /opt/google/chrome/chrome`` should show
  ``-rwxr-xr-x ... root root``; re-run step 2.
- ``Missing X server`` from the plugin → ``$DISPLAY`` isn't
  exported in the shell the plugin reads from. The plugin runs
  out-of-process so ``DISPLAY=:99 some-mcp-tool ...`` doesn't
  reach it; you need ``export DISPLAY=:99`` in the parent shell
  *before* the agent starts the chrome session, OR add it to
  ``~/.bashrc`` (step 3) and start a fresh shell.
- ``Target closed`` immediately after ``new_page`` → browser
  process crashed. Check ``ps aux | grep chrom`` — if it's gone,
  re-run ``DISPLAY=:99 /opt/google/chrome/chrome --version`` to
  get the underlying error.
- ``Could not find Google Chrome`` after a devcontainer rebuild
  → the rebuild wiped ``/opt/google/chrome/``. Re-run step 2.
- The plugin says nothing's wrong but every screenshot is blank
  → the headless fallback (Option B above) was used instead of
  the wrapper; the wrapper expects to be called via ``$DISPLAY``
  and produces correctly-rendered pages. Re-export
  ``DISPLAY=:99`` and retry.

## Files this skill touches

- ``/opt/google/chrome/chrome`` (wrapper script — added by step 2).
- ``/tmp/.X11-unix/`` (X socket dir — created by step 3).
- ``~/.bashrc`` (only if you opt into the persistent ``DISPLAY``
  export).

Nothing in the project tree is modified; this is an
environment-bootstrap skill.

## Why not bake this into the Dockerfile?

The devcontainer image is shared with non-AI users; pre-installing
xvfb + the chrome wrapper in the image isn't strictly required for
human developers (they have a real browser on their host). The
trade-off is having one skill that's idempotent and runs in
seconds vs. shipping infrastructure most users won't use. If we
ever decide to bake it in, the recipe is in the four steps above —
they translate directly to a ``RUN`` block.
