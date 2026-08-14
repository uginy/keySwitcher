#!/bin/bash
# KeySwitcher — quick developer update script.
# Rebuilds the Swift bundle, bundles engine from engine/, replaces
# the installed application, and restarts the running instance.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENGINE_SRC="$SCRIPT_DIR/engine/keyswitcher.py"
ROTATOR_SRC="$SCRIPT_DIR/engine/rotator.py"
APP_SRC="$SCRIPT_DIR/dist/KeySwitcher.app"
ENGINE_DIR="$HOME/.codex/keyswitcher"

cleanup_legacy_codex_files() {
    pkill -f "$HOME/.codex/daemon.py" >/dev/null 2>&1 || true
    rm -rf "$ENGINE_DIR/__pycache__"
    rm -f \
        "$HOME/.codex/rotate.py" \
        "$HOME/.codex/daemon.py" \
        "$HOME/.codex/daemon.log" \
        "$HOME/.codex/daemon.out.log" \
        "$HOME/.codex/daemon.err.log"
}

echo "==> Dev update: building KeySwitcher.app ..."
(cd "$SCRIPT_DIR" && ./build.sh)

if [[ ! -f "$ENGINE_SRC" ]]; then
    echo "ERROR: engine not found at $ENGINE_SRC" >&2
    exit 1
fi
if [[ ! -f "$ROTATOR_SRC" ]]; then
    echo "ERROR: rotator not found at $ROTATOR_SRC" >&2
    exit 1
fi
if [[ ! -d "$APP_SRC" ]]; then
    echo "ERROR: build failed to create application at $APP_SRC" >&2
    exit 1
fi

echo "==> Engine and rotator are inside rebuilt KeySwitcher.app"
echo "==> Removing legacy copies from $ENGINE_DIR (if any remain) ..."
rm -f "$ENGINE_DIR/keyswitcher.py" "$ENGINE_DIR/rotator.py"
echo "==> Cleaning legacy Python files from ~/.codex"
cleanup_legacy_codex_files

if [[ -n "${KEYSWITCHER_APP_DIR:-}" ]]; then
    APP_DEST_DIR="$KEYSWITCHER_APP_DIR"
    mkdir -p "$APP_DEST_DIR"
elif [[ -d "/Applications/KeySwitcher.app" || -w /Applications ]]; then
    APP_DEST_DIR="/Applications"
else
    APP_DEST_DIR="$HOME/Applications"
    mkdir -p "$APP_DEST_DIR"
fi
APP_DEST="$APP_DEST_DIR/KeySwitcher.app"

echo "==> Replacing application at: $APP_DEST"
osascript -e 'tell application "KeySwitcher" to quit' >/dev/null 2>&1 || true
pkill -x KeySwitcher >/dev/null 2>&1 || true
rm -rf "$APP_DEST"
ditto "$APP_SRC" "$APP_DEST"
chflags nohidden "$APP_DEST" 2>/dev/null || true
touch "$APP_DEST"

echo "==> Launching updated KeySwitcher..."
open "$APP_DEST"

echo "==> Done: fresh bundle installed and menu bar app restarted."
