#!/bin/bash
# Captain Speaking: start the server.
# Double-click this file in Finder, or run ./start.command in a terminal.
#   ./start.command --setup   only create the virtualenv and install packages
# Settings (environment variables):
#   PORT=8788        plain HTTP port (default 8787). Speakers download announcements from this one.
#   HTTPS_PORT=8444  secure port for phones (default 8443). HTTPS_PORT=0 turns the secure address off.
set -e
cd "$(dirname "$0")"
PORT="${PORT:-8787}"
HTTPS_PORT="${HTTPS_PORT:-8443}"

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

for p in "$PORT" "$HTTPS_PORT"; do
  if [ "$p" != "0" ] && lsof -nP -iTCP:"$p" -sTCP:LISTEN >/dev/null 2>&1; then
    echo "Port $p is already in use. Captain Speaking may already be running."
    echo "Stop it first (see the README), or choose other ports: PORT=8788 HTTPS_PORT=8444 ./start.command"
    exit 1
  fi
done

IP=$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || echo "")

# Phones only allow microphone access on secure pages, so serve a second, HTTPS address.
# The certificate is self-signed and made for this computer's current IP; it is created once
# and renewed when it expires or the IP changes. It lives in certs/ (never committed).
if [ "$HTTPS_PORT" != "0" ]; then
  mkdir -p certs
  need_cert=0
  [ -f certs/cert.pem ] && [ -f certs/key.pem ] || need_cert=1
  if [ "$need_cert" = 0 ]; then
    openssl x509 -in certs/cert.pem -noout -checkend 2592000 >/dev/null 2>&1 || need_cert=1
    if [ -n "$IP" ]; then
      openssl x509 -in certs/cert.pem -noout -text 2>/dev/null | grep -q "IP Address:$IP" || need_cert=1
    fi
  fi
  if [ "$need_cert" = 1 ]; then
    echo "Creating a certificate for the secure address..."
    SAN="DNS:localhost,IP:127.0.0.1"
    [ -n "$IP" ] && SAN="$SAN,IP:$IP"
    openssl req -x509 -newkey rsa:2048 -nodes -days 825 \
      -keyout certs/key.pem -out certs/cert.pem \
      -subj "/CN=Captain Speaking" -addext "subjectAltName=$SAN" >/dev/null 2>&1
  fi
fi

echo ""
echo "Captain Speaking is starting."
echo "  On this computer:   http://localhost:$PORT"
echo "  On your network:    http://${IP:-<your-computer-ip>}:$PORT"
if [ "$HTTPS_PORT" != "0" ]; then
  echo "  On your phone:      https://${IP:-<your-computer-ip>}:$HTTPS_PORT   (use this one to record announcements)"
  echo "                      Accept the one-time security warning: the certificate is your own."
fi
echo "Press Ctrl-C to stop."
echo ""

# Speakers download announcements from the plain HTTP port, so it must match the one we serve on
export PUBLIC_HTTP_PORT="$PORT"
export HTTPS_PORT

if [ "$HTTPS_PORT" = "0" ]; then
  exec .venv/bin/uvicorn app:app --host 0.0.0.0 --port "$PORT" --timeout-graceful-shutdown 3
fi

.venv/bin/uvicorn app:app --host 0.0.0.0 --port "$PORT" --timeout-graceful-shutdown 3 &
HTTP_PID=$!
.venv/bin/uvicorn app:app --host 0.0.0.0 --port "$HTTPS_PORT" \
  --ssl-keyfile certs/key.pem --ssl-certfile certs/cert.pem --log-level warning --timeout-graceful-shutdown 3 &
HTTPS_PID=$!

stop_all() { kill "$HTTP_PID" "$HTTPS_PID" 2>/dev/null || true; }
trap 'stop_all; exit 0' INT TERM
trap stop_all EXIT

# If either listener dies, stop the other and exit, so a login item restarts them together
while kill -0 "$HTTP_PID" 2>/dev/null && kill -0 "$HTTPS_PID" 2>/dev/null; do
  sleep 2
done
exit 1
