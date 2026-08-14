#!/bin/bash
# KeySwitcher — uninstallation script.
# Removes the application bundle and runtime metadata directory (~/.codex/keyswitcher/).
# Preserves token slots in ~/.codex/accounts/ and active ~/.codex/auth.json.
# Login item registration is automatically revoked upon application removal.
set -uo pipefail

REMOVED=()
KEPT=(
    "~/.codex/accounts/ (account token slots)"
    "~/.codex/auth.json"
)

echo "==> Stopping KeySwitcher..."
osascript -e 'tell application "KeySwitcher" to quit' >/dev/null 2>&1 || true
pkill -x KeySwitcher >/dev/null 2>&1 || true
launchctl bootout "gui/$(id -u)/com.codex.rotator" >/dev/null 2>&1 \
    || launchctl unload "$HOME/Library/LaunchAgents/com.codex.rotator.plist" >/dev/null 2>&1 || true
pkill -f "$HOME/Library/Application Support/KeySwitcher/rotator.py" >/dev/null 2>&1 || true
sleep 1

for APP in "/Applications/KeySwitcher.app" "$HOME/Applications/KeySwitcher.app"; do
    if [[ -d "$APP" ]]; then
        rm -rf "$APP"
        REMOVED+=("$APP")
    fi
done

if [[ -d "$HOME/.codex/keyswitcher" ]]; then
    rm -rf "$HOME/.codex/keyswitcher/__pycache__"
    rm -rf "$HOME/.codex/keyswitcher"
    REMOVED+=("$HOME/.codex/keyswitcher/ (runtime metadata: config/cache/state)")
fi

for FILE in "$HOME/.codex/rotate.py" "$HOME/.codex/daemon.py" "$HOME/.codex/daemon.log" "$HOME/.codex/daemon.out.log" "$HOME/.codex/daemon.err.log"; do
    if [[ -f "$FILE" ]]; then
        rm -f "$FILE"
        REMOVED+=("$FILE")
    fi
done

if [[ -d "$HOME/Library/Application Support/KeySwitcher" ]]; then
    rm -rf "$HOME/Library/Application Support/KeySwitcher"
    REMOVED+=("$HOME/Library/Application Support/KeySwitcher/")
fi

if [[ -f "$HOME/Library/LaunchAgents/com.codex.rotator.plist" ]]; then
    rm -f "$HOME/Library/LaunchAgents/com.codex.rotator.plist"
    REMOVED+=("$HOME/Library/LaunchAgents/com.codex.rotator.plist")
fi

echo
if [[ ${#REMOVED[@]} -gt 0 ]]; then
    echo "Removed:"
    for ITEM in "${REMOVED[@]}"; do
        echo "  - $ITEM"
    done
else
    echo "Nothing to remove: KeySwitcher was not installed."
fi

echo
echo "Intentionally preserved:"
for ITEM in "${KEPT[@]}"; do
    echo "  - $ITEM"
done
echo
echo "Launch at login item is automatically removed."
