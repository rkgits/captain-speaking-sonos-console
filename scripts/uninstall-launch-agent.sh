#!/bin/bash
# Stop Captain Speaking and remove it from login items.
# This does not delete the project folder or your saved recordings.
set -e
LABEL="${LABEL:-com.captainspeaking.app}"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
rm -f "$PLIST"
echo "Captain Speaking no longer starts at login, and the background copy is stopped."
echo "To delete everything, remove this project folder (it includes your saved recordings)."
