#!/bin/zsh
# FLOWS — double-click launcher (macOS)
# Finder runs .command files in Terminal on double-click. This starts the hub
# if it isn't already up, then opens it in your default browser.
cd "$(dirname "$0")"

if ! lsof -iTCP:8787 -sTCP:LISTEN -n >/dev/null 2>&1; then
  echo "Starting FLOWS…"
  python3 social-hub/server.py >/tmp/flows.log 2>&1 &
  sleep 1
else
  echo "FLOWS is already running."
fi

open "http://localhost:8787"
echo "FLOWS is open at http://localhost:8787"
echo "Logs: /tmp/flows.log    Stop: pkill -f social-hub/server.py"
