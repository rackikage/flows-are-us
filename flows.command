#!/bin/zsh
# FLOWS — double-click launcher (macOS)
# Finder runs .command files in Terminal on double-click. This starts the hub
# if it isn't already up, then opens it in your default browser.
cd "$(dirname "$0")"

# Pick a Python that actually has the deps (plain `python3` may not).
PY=""
for cand in /Library/Frameworks/Python.framework/Versions/3.14/bin/python3 \
            /opt/homebrew/bin/python3 /usr/local/bin/python3 python3; do
  if command -v "$cand" >/dev/null 2>&1 && \
     "$cand" -c 'import fastapi, uvicorn' >/dev/null 2>&1; then
    PY="$cand"; break
  fi
done
if [ -z "$PY" ]; then
  echo "No Python with FastAPI found. Run: pip3 install fastapi uvicorn requests"
  read -r "?Press Enter to close…"; exit 1
fi

if ! lsof -iTCP:8787 -sTCP:LISTEN -n >/dev/null 2>&1; then
  echo "Starting FLOWS…"
  "$PY" social-hub/server.py >/tmp/flows.log 2>&1 &
  for i in {1..15}; do
    curl -sf -o /dev/null http://127.0.0.1:8787/api/accounts && break
    sleep 1
  done
else
  echo "FLOWS is already running."
fi

open "http://localhost:8787"
echo "FLOWS is open at http://localhost:8787"
echo "Logs: /tmp/flows.log    Stop: pkill -f social-hub/server.py"
