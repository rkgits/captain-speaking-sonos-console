#!/bin/bash
# Start Captain Speaking automatically at login (and restart it if it stops).
# Usage: scripts/install-launch-agent.sh
# Undo:  scripts/uninstall-launch-agent.sh
set -e
DIR="$(cd "$(dirname "$0")/.." && pwd)"
LABEL="${LABEL:-com.captainspeaking.app}"
PORT="${PORT:-8787}"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG="$HOME/Library/Logs/captain-speaking.log"

# macOS blocks background login items from reading Documents, Desktop, Downloads and iCloud Drive,
# so the server would silently fail to start from there. Refuse with a clear explanation.
case "$DIR" in
  "$HOME/Documents"*|"$HOME/Desktop"*|"$HOME/Downloads"*|*"/Library/Mobile Documents/"*)
    echo "This folder is inside a location macOS protects from background apps:"
    echo "  $DIR"
    echo "Move the project somewhere else (for example your home folder: ~/captain-speaking),"
    echo "then run this script again from there."
    exit 1 ;;
esac

# Make sure the virtualenv and packages are ready before launchd starts the server
"$DIR/start.command" --setup
mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"

cat > "$PLIST" <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array><string>/bin/bash</string><string>$DIR/start.command</string></array>
  <key>WorkingDirectory</key><string>$DIR</string>
  <key>EnvironmentVariables</key>
  <dict><key>PORT</key><string>$PORT</string></dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$LOG</string>
  <key>StandardErrorPath</key><string>$LOG</string>
</dict>
</plist>
PLISTEOF

# Reload if it was already installed, then start it
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"

echo "Installed. Captain Speaking now starts at login."
echo "  Open:  http://localhost:$PORT"
echo "  Log:   $LOG"
echo "  Stop and remove it with: scripts/uninstall-launch-agent.sh"
