#!/usr/bin/env bash
# Runs every time the devcontainer starts (initial create AND every
# subsequent VS Code attach that boots the container).
#
# Xvfb is a foreground process with no init system behind it inside
# the container, so it has to be (re)started on every container boot.
# The ``pgrep`` guard makes this a no-op when Xvfb is already running
# (e.g. on subsequent VS Code window attaches that don't restart the
# container).
#
# ``:99`` matches the ``DISPLAY=:99`` value exported from
# ``containerEnv``. ``-nolisten tcp`` keeps the X socket purely local
# so nothing outside the container can attach to the display.

set -euo pipefail

if ! pgrep -x Xvfb >/dev/null 2>&1; then
	Xvfb :99 -screen 0 1280x900x24 -nolisten tcp >/tmp/xvfb.log 2>&1 &
fi
