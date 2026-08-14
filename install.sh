#!/bin/bash
# KeySwitcher — automated installer.
# Idempotent: running again rebuilds the app (engine bundled inside)
# and reinstalls it in-place.
#
# Standard mode preserves: ~/.codex/accounts/ and ~/.codex/auth.json.
# The engine and rotator are bundled INSIDE KeySwitcher.app (compiled from engine/);
# only runtime state remains in ~/.codex/keyswitcher/ (config/cache/state).
# The --portable flag moves ~/.codex to ./runtime/codex and creates a symlink.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENGINE_SRC="$SCRIPT_DIR/engine/keyswitcher.py"
ROTATOR_SRC="$SCRIPT_DIR/engine/rotator.py"
APP_SRC="$SCRIPT_DIR/dist/KeySwitcher.app"
ENGINE_DIR="$HOME/.codex/keyswitcher"
PORTABLE_CODEX_DIR="$SCRIPT_DIR/runtime/codex"
PORTABLE=false

cleanup_legacy_codex_files() {
    launchctl unload "$HOME/Library/LaunchAgents/com.codex.rotator.plist" >/dev/null 2>&1 || true
    pkill -f "$HOME/.codex/daemon.py" >/dev/null 2>&1 || true
    pkill -f "rotator.py daemon" >/dev/null 2>&1 || true
    rm -rf "$ENGINE_DIR/__pycache__"
    rm -f \
        "$HOME/Library/LaunchAgents/com.codex.rotator.plist" \
        "$HOME/Library/Application Support/KeySwitcher/rotator.py" \
        "$HOME/.codex/rotate.py" \
        "$HOME/.codex/daemon.py" \
        "$HOME/.codex/daemon.log" \
        "$HOME/.codex/daemon.out.log" \
        "$HOME/.codex/daemon.err.log"
    rm -rf "$HOME/Library/Logs/KeySwitcher"
}

if [[ "${1:-}" == "--portable" ]]; then
    PORTABLE=true
fi

setup_portable_codex_home() {
    echo "==> Portable mode: runtime Codex home: $PORTABLE_CODEX_DIR"
    mkdir -p "$SCRIPT_DIR/runtime"

    if [[ -L "$HOME/.codex" ]]; then
        local target
        target="$(readlink "$HOME/.codex")"
        if [[ "$target" == "$PORTABLE_CODEX_DIR" ]]; then
            echo "==> ~/.codex already points to portable runtime"
            mkdir -p "$PORTABLE_CODEX_DIR"
            chmod 700 "$PORTABLE_CODEX_DIR"
            return
        fi
        echo "ERROR: ~/.codex is already a symlink pointing to another location: $target" >&2
        exit 1
    fi

    if [[ -e "$HOME/.codex" ]]; then
        if [[ -e "$PORTABLE_CODEX_DIR" ]]; then
            echo "ERROR: both ~/.codex and $PORTABLE_CODEX_DIR exist — cannot safely merge automatically" >&2
            exit 1
        fi
        echo "==> Migrating ~/.codex -> $PORTABLE_CODEX_DIR"
        mv "$HOME/.codex" "$PORTABLE_CODEX_DIR"
    else
        mkdir -p "$PORTABLE_CODEX_DIR"
    fi

    chmod 700 "$PORTABLE_CODEX_DIR"
    ln -s "$PORTABLE_CODEX_DIR" "$HOME/.codex"
    echo "==> Created symlink: ~/.codex -> $PORTABLE_CODEX_DIR"
}

if [[ "$PORTABLE" == true ]]; then
    setup_portable_codex_home
fi

echo "==> Building KeySwitcher.app (build.sh)..."
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

echo "==> Engine and rotator are bundled inside KeySwitcher.app (from engine/)"
echo "==> Cleaning old copies from $ENGINE_DIR (if any remain) ..."
rm -f "$ENGINE_DIR/keyswitcher.py" "$ENGINE_DIR/rotator.py"
cleanup_legacy_codex_files

# Choose application destination directory: /Applications if writable, else ~/Applications.
if [[ -w /Applications ]]; then
    APP_DEST_DIR="/Applications"
else
    APP_DEST_DIR="$HOME/Applications"
    mkdir -p "$APP_DEST_DIR"
    echo "==> /Applications is not writable, installing to $APP_DEST_DIR"
fi
APP_DEST="$APP_DEST_DIR/KeySwitcher.app"

echo "==> Installing application to $APP_DEST ..."
# Quit running instance to replace bundle safely.
osascript -e 'tell application "KeySwitcher" to quit' >/dev/null 2>&1 || true
pkill -x KeySwitcher >/dev/null 2>&1 || true
sleep 1
rm -rf "$APP_DEST"
ditto "$APP_SRC" "$APP_DEST"
chflags nohidden "$APP_DEST" 2>/dev/null || true

echo "==> Launching KeySwitcher..."
open "$APP_DEST"

echo
echo "Done. KeySwitcher is installed and running in the menu bar."
echo
echo "Next steps:"
echo "  1. Allow Notifications: System Settings -> Notifications -> KeySwitcher."
echo "  2. Optionally enable 'Launch at Login' in the panel settings."
echo "  3. To enable graceful Codex window reload (Cmd+R) after switching:"
echo "     System Settings -> Privacy & Security -> Accessibility -> add KeySwitcher."
