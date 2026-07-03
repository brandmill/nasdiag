#!/bin/bash
# COPYMILL launcher — double-click to start, keep this window open while
# using the app. Launching from Terminal matters: macOS only grants
# Local Network access (needed for the network speed test) to apps
# started from a proper user session, not to orphaned background
# processes.
cd "$HOME/copymill" || { echo "copymill not found at ~/copymill"; read -r; exit 1; }
if [ -z "$COPYMILL_FILESAFE" ]; then
  for c in "$HOME/filesafe" "$HOME/filesafe-fix/filesafe"; do
    [ -x "$c" ] && export COPYMILL_FILESAFE="$c" && break
  done
fi
echo "COPYMILL — starting (filesafe: ${COPYMILL_FILESAFE:-from PATH})"
exec python3 -m copymill --host 0.0.0.0
