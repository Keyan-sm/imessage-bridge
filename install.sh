#!/bin/bash
# imessage-bridge installer - run on the Mac Mini that hosts Messages.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
INSTALL_DIR="$HOME/imessage-bridge"
PLIST_NAME="com.imessagebridge.plist"
LA_DIR="$HOME/Library/LaunchAgents"

echo "==> Installing bridge to $INSTALL_DIR"
mkdir -p "$INSTALL_DIR"
cp "$REPO_DIR/bridge.py" "$INSTALL_DIR/bridge.py"
chmod +x "$INSTALL_DIR/bridge.py"

if [ ! -f "$INSTALL_DIR/config.json" ]; then
    cp "$REPO_DIR/config.example.json" "$INSTALL_DIR/config.json"
    echo "==> Created $INSTALL_DIR/config.json - EDIT IT: set drive_folder to your Google Drive folder."
    echo "    Usually: ~/Library/CloudStorage/GoogleDrive-<you@gmail.com>/My Drive/imessage-bridge"
    ls "$HOME/Library/CloudStorage/" 2>/dev/null || true
else
    echo "==> Keeping existing config.json"
fi

echo "==> Installing launchd agent"
mkdir -p "$LA_DIR"
sed "s|__HOME__|$HOME|g" "$REPO_DIR/$PLIST_NAME" > "$LA_DIR/$PLIST_NAME"
launchctl bootout "gui/$(id -u)/com.imessagebridge" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$LA_DIR/$PLIST_NAME"

cat <<'DONE'

Done. Two manual steps remain:

1. Full Disk Access (required, or chat.db reads fail):
   System Settings > Privacy & Security > Full Disk Access > +
   Press Cmd+Shift+G, enter /usr/bin/python3, add it and switch it on.
   (Also add Terminal/iTerm if you want to run bridge.py by hand for testing.)

2. Edit ~/imessage-bridge/config.json and point drive_folder at your
   Google Drive desktop folder, then restart the agent:
     launchctl kickstart -k gui/$(id -u)/com.imessagebridge

Test first without launchd:
     /usr/bin/python3 ~/imessage-bridge/bridge.py ~/imessage-bridge/config.json

Logs: ~/Library/Logs/imessage-bridge.log
DONE
