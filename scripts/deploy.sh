#!/bin/bash
# Usage: scripts/deploy.sh [staging|production]
# Called by GitHub Actions self-hosted runner on this Mac.
set -e

ENV="${1:-staging}"

case "$ENV" in
  staging)
    DEPLOY_DIR="$HOME/sonos-app-staging"
    LAUNCHAGENT="com.sonos-app-staging"
    PORT=8788
    ;;
  production)
    DEPLOY_DIR="$HOME/sonos-app"
    LAUNCHAGENT="com.sonos-app"
    PORT=8787
    ;;
  *)
    echo "Usage: deploy.sh [staging|production]"
    exit 1
    ;;
esac

RUNNER_WORKSPACE="${GITHUB_WORKSPACE:-$(pwd)}"
RELEASES_DIR="$HOME/sonos-releases/$ENV"

echo "==> Deploying to $ENV ($DEPLOY_DIR, port $PORT)"

# 1. Snapshot current deployment before overwriting (production only)
if [ "$ENV" = "production" ] && [ -d "$DEPLOY_DIR/app.py" -o -f "$DEPLOY_DIR/app.py" ]; then
  SNAPSHOT="$RELEASES_DIR/$(date +%Y%m%d_%H%M%S)"
  mkdir -p "$SNAPSHOT"
  rsync -a \
    --exclude='.git/' --exclude='.venv/' \
    --exclude='__pycache__/' --exclude='*.log' \
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
  "$RUNNER_WORKSPACE/" "$DEPLOY_DIR/"

# 3. Recreate run_server.sh (env-specific, not in git)
cat > "$DEPLOY_DIR/run_server.sh" << SCRIPT
#!/bin/bash
cd $DEPLOY_DIR
echo "[\$(date)] Starting $ENV server..." >> $DEPLOY_DIR/server.log 2>&1
exec /usr/bin/caffeinate -s $DEPLOY_DIR/.venv/bin/uvicorn app:app --host 0.0.0.0 --port $PORT
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
