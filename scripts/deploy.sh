#!/bin/bash
# Usage: scripts/deploy.sh [staging|production]
# Run by hand from a clone of the repository on the Mac that will serve the app.
set -e

ENV="${1:-staging}"

case "$ENV" in
  staging)
    DEPLOY_DIR="$HOME/sonos/staging"
    LAUNCHAGENT="com.sonos-app-staging"
    PORT=8788
    HTTPS_PORT=8444
    ;;
  production)
    DEPLOY_DIR="$HOME/sonos/production"
    LAUNCHAGENT="com.sonos-app"
    PORT=8787
    HTTPS_PORT=8443
    ;;
  *)
    echo "Usage: deploy.sh [staging|production]"
    exit 1
    ;;
esac

SOURCE_DIR="$(pwd)"
RELEASES_DIR="$HOME/sonos/releases/$ENV"

echo "==> Deploying to $ENV ($DEPLOY_DIR, port $PORT)"

# 1. Snapshot current deployment before overwriting (production only)
if [ "$ENV" = "production" ] && [ -d "$DEPLOY_DIR/app.py" -o -f "$DEPLOY_DIR/app.py" ]; then
  SNAPSHOT="$RELEASES_DIR/$(date +%Y%m%d_%H%M%S)"
  mkdir -p "$SNAPSHOT"
  rsync -a \
    --exclude='.git/' --exclude='.venv/' \
    --exclude='__pycache__/' --exclude='*.log' \
    --exclude='certs/' --exclude='recordings/' \
    "$DEPLOY_DIR/" "$SNAPSHOT/"
  echo "==> Snapshot saved → $SNAPSHOT"
  # Keep only the 3 most recent snapshots
  ls -dt "$RELEASES_DIR"/*/  2>/dev/null | tail -n +4 | xargs rm -rf
  echo "==> Releases kept: $(ls "$RELEASES_DIR" | wc -l | tr -d ' ')"
fi

# 2. Sync code (exclude venv, logs, git internals)
rsync -a --delete \
  --exclude='.git/' \
  --exclude='.venv/' \
  --exclude='__pycache__/' \
  --exclude='*.log' \
  --exclude='.pytest_cache/' \
  --exclude='certs/' \
  --exclude='recordings/' \
  "$SOURCE_DIR/" "$DEPLOY_DIR/"

# 2b. Ensure recordings dir (persists across deploys; excluded from rsync)
mkdir -p "$DEPLOY_DIR/recordings"

# 2c. Generate a self-signed cert once (for the HTTPS listener; excluded from rsync)
CERTS="$DEPLOY_DIR/certs"
if [ ! -f "$CERTS/cert.pem" ]; then
  echo "==> Generating self-signed TLS cert (10y)"
  mkdir -p "$CERTS"
  LAN_IP=$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || echo 127.0.0.1)
  openssl req -x509 -newkey rsa:2048 -nodes -days 3650 \
    -keyout "$CERTS/key.pem" -out "$CERTS/cert.pem" \
    -subj "/CN=sonos.local" \
    -addext "subjectAltName=DNS:sonos.local,DNS:localhost,IP:$LAN_IP,IP:127.0.0.1" 2>/dev/null
fi

# 3. Recreate run_server.sh (env-specific, not in git) — runs HTTP + HTTPS side by side.
cat > "$DEPLOY_DIR/run_server.sh" << SCRIPT
#!/bin/bash
cd $DEPLOY_DIR
echo "[\$(date)] Starting $ENV server (HTTP $PORT + HTTPS $HTTPS_PORT)..." >> $DEPLOY_DIR/server.log 2>&1
export PUBLIC_HTTP_PORT=$PORT
export HTTPS_PORT=$HTTPS_PORT
/usr/bin/caffeinate -s $DEPLOY_DIR/.venv/bin/uvicorn app:app --host 0.0.0.0 --port $PORT >> $DEPLOY_DIR/server.log 2>&1 &
HTTP_PID=\$!
/usr/bin/caffeinate -s $DEPLOY_DIR/.venv/bin/uvicorn app:app --host 0.0.0.0 --port $HTTPS_PORT \\
  --ssl-keyfile $DEPLOY_DIR/certs/key.pem --ssl-certfile $DEPLOY_DIR/certs/cert.pem >> $DEPLOY_DIR/server.log 2>&1 &
HTTPS_PID=\$!
# Supervise both listeners (bash 3.2 has no 'wait -n'): if either dies, kill the
# other and exit so launchd's KeepAlive restarts the pair.
while kill -0 \$HTTP_PID 2>/dev/null && kill -0 \$HTTPS_PID 2>/dev/null; do
  sleep 5
done
kill \$HTTP_PID \$HTTPS_PID 2>/dev/null
exit 1
SCRIPT
chmod +x "$DEPLOY_DIR/run_server.sh"

# 3. Rebuild venv if requirements changed
VENV="$DEPLOY_DIR/.venv"
REQ_HASH_FILE="$DEPLOY_DIR/.req_hash"
REQ_HASH=$(sha256sum "$DEPLOY_DIR/requirements.txt" | awk '{print $1}')

if [ ! -d "$VENV" ] || [ "$(cat "$REQ_HASH_FILE" 2>/dev/null)" != "$REQ_HASH" ]; then
  echo "==> Installing/updating dependencies"
  python3 -m venv "$VENV"
  "$VENV/bin/pip" install -q -r "$DEPLOY_DIR/requirements.txt"
  echo "$REQ_HASH" > "$REQ_HASH_FILE"
fi

# 4. Restart the server
echo "==> Restarting $LAUNCHAGENT"
launchctl kickstart -k "gui/$(id -u)/$LAUNCHAGENT" 2>/dev/null || \
  launchctl start "gui/$(id -u)/$LAUNCHAGENT" 2>/dev/null || true

# 5. Wait for server to come up
echo "==> Waiting for server on port $PORT..."
for i in $(seq 1 15); do
  if curl -sf "http://localhost:$PORT/state" > /dev/null 2>&1; then
    echo "==> Server is up"
    break
  fi
  sleep 1
done

# 6. On staging: run the test suite
if [ "$ENV" = "staging" ]; then
  echo "==> Running tests against staging (port $PORT)"
  TEST_URL="http://localhost:$PORT" \
    "$VENV/bin/pytest" tests/ -v --tb=short
  echo "==> All tests passed"
fi

echo "==> Deploy to $ENV complete"
