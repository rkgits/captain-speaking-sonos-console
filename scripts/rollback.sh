#!/bin/bash
# Roll production back to the previous release.
# Usage: bash scripts/rollback.sh
set -e

DEPLOY_DIR="$HOME/sonos-app"
RELEASES_DIR="$HOME/sonos-releases/production"
LAUNCHAGENT="com.sonos-app"
PORT=8787

# List available snapshots
echo "==> Available snapshots:"
ls -dt "$RELEASES_DIR"/*/ 2>/dev/null | while read dir; do
  echo "    $(basename "$dir")"
done

# Pick the most recent snapshot
LAST_RELEASE=$(ls -dt "$RELEASES_DIR"/*/ 2>/dev/null | head -1)

if [ -z "$LAST_RELEASE" ]; then
  echo "ERROR: No snapshots found in $RELEASES_DIR — nothing to roll back to."
  exit 1
fi

echo ""
echo "==> Rolling back to: $(basename "$LAST_RELEASE")"
read -p "    Continue? [y/N] " confirm
if [[ "$confirm" != "y" && "$confirm" != "Y" ]]; then
  echo "Aborted."
  exit 0
fi

# Restore snapshot to production
rsync -a --delete \
  --exclude='.venv/' \
  --exclude='*.log' \
  "$LAST_RELEASE" "$DEPLOY_DIR/"

# Recreate run_server.sh (env-specific)
cat > "$DEPLOY_DIR/run_server.sh" << SCRIPT
#!/bin/bash
cd $DEPLOY_DIR
echo "[\$(date)] Starting production server (rollback)..." >> $DEPLOY_DIR/server.log 2>&1
exec /usr/bin/caffeinate -s $DEPLOY_DIR/.venv/bin/uvicorn app:app --host 0.0.0.0 --port $PORT
SCRIPT
chmod +x "$DEPLOY_DIR/run_server.sh"

# Restart server
echo "==> Restarting production server..."
launchctl kickstart -k "gui/$(id -u)/$LAUNCHAGENT" 2>/dev/null || true

# Wait for it
for i in $(seq 1 15); do
  if curl -sf "http://localhost:$PORT/state" > /dev/null 2>&1; then
    echo "==> Server is up. Rollback complete!"
    echo ""
    echo "    Production is now running: $(basename "$LAST_RELEASE")"
    echo "    Remove this snapshot if you want to prevent rolling back to it again:"
    echo "    rm -rf $LAST_RELEASE"
    exit 0
  fi
  sleep 1
done

echo "ERROR: Server did not come up after rollback. Check logs:"
echo "  tail -30 $DEPLOY_DIR/server.log"
exit 1
