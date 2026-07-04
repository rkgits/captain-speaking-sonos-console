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

echo "==> Deploying to $ENV ($DEPLOY_DIR, port $PORT)"

# 1. Sync code (exclude venv, logs, git internals)
rsync -a --delete \
  --exclude='.git/' \
  --exclude='.venv/' \
  --exclude='__pycache__/' \
  --exclude='*.log' \
  --exclude='.pytest_cache/' \
  "$RUNNER_WORKSPACE/" "$DEPLOY_DIR/"

# 2. Rebuild venv if requirements changed
VENV="$DEPLOY_DIR/.venv"
REQ_HASH_FILE="$DEPLOY_DIR/.req_hash"
REQ_HASH=$(sha256sum "$DEPLOY_DIR/requirements.txt" | awk '{print $1}')

if [ ! -d "$VENV" ] || [ "$(cat "$REQ_HASH_FILE" 2>/dev/null)" != "$REQ_HASH" ]; then
  echo "==> Installing/updating dependencies"
  python3 -m venv "$VENV"
  "$VENV/bin/pip" install -q -r "$DEPLOY_DIR/requirements.txt"
  echo "$REQ_HASH" > "$REQ_HASH_FILE"
fi

# 3. Restart the server
echo "==> Restarting $LAUNCHAGENT"
launchctl kickstart -k "gui/$(id -u)/$LAUNCHAGENT" 2>/dev/null || \
  launchctl start "gui/$(id -u)/$LAUNCHAGENT" 2>/dev/null || true

# Wait for server to come up
echo "==> Waiting for server on port $PORT..."
for i in $(seq 1 15); do
  if curl -sf "http://localhost:$PORT/state" > /dev/null 2>&1; then
    echo "==> Server is up"
    break
  fi
  sleep 1
done

# 4. On staging: run the test suite
if [ "$ENV" = "staging" ]; then
  echo "==> Running tests against staging (port $PORT)"
  TEST_URL="http://localhost:$PORT" \
    "$VENV/bin/pytest" tests/ -v --tb=short
  echo "==> All tests passed"
fi

echo "==> Deploy to $ENV complete"
