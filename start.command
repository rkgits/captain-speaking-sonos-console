#!/bin/bash
# Captain Speaking: start the server.
# Double-click this file in Finder, or run ./start.command in a terminal.
#   ./start.command --setup   only create the virtualenv and install packages
# Set PORT to use a port other than 8787.
set -e
cd "$(dirname "$0")"
PORT="${PORT:-8787}"

if ! command -v python3 >/dev/null 2>&1; then
  echo "Python 3 is required. Install it with 'brew install python' or the Xcode Command Line Tools."
  exit 1
fi

# Create the virtualenv and (re)install packages when requirements.txt changes
[ -d .venv ] || python3 -m venv .venv
HASH=$(shasum -a 256 requirements.txt | awk '{print $1}')
if [ "$(cat .venv/.req_hash 2>/dev/null)" != "$HASH" ]; then
  echo "Installing packages..."
  .venv/bin/pip install -q -r requirements.txt
  echo "$HASH" > .venv/.req_hash
fi
[ "$1" = "--setup" ] && exit 0

if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "Port $PORT is already in use. Captain Speaking may already be running."
  echo "Stop it first (see the README), or start on another port: PORT=8788 ./start.command"
  exit 1
fi

IP=$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || echo "<your-computer-ip>")
echo ""
echo "Captain Speaking is starting."
echo "  On this computer:  http://localhost:$PORT"
echo "  On your network:   http://$IP:$PORT"
echo "Press Ctrl-C to stop."
echo ""

# Speakers download announcements from this port, so it must match the one we serve on
export PUBLIC_HTTP_PORT="$PORT"
exec .venv/bin/uvicorn app:app --host 0.0.0.0 --port "$PORT"
