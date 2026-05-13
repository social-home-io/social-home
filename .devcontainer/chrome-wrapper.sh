#!/bin/bash
# Wraps Google Chrome with flags required to run inside this devcontainer.
#
# --no-sandbox: unprivileged user-namespace clone (CLONE_NEWUSER) isn't
# permitted in the container, so Chrome's setuid sandbox can't initialise.
# Safe here because we only ever inspect our own local services.
#
# --disable-dev-shm-usage: /dev/shm is tiny in containers; routing shared
# memory through /tmp avoids OOM-style crashes on heavy pages.
exec /opt/google/chrome/chrome.real --no-sandbox --disable-dev-shm-usage "$@"
