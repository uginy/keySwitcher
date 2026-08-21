#!/bin/bash
#
# Build KeySwitcher.app into dist/.
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$ROOT/dist/KeySwitcher.app"
CONTENTS="$APP_DIR/Contents"
MACOS_DIR="$CONTENTS/MacOS"
RESOURCES_DIR="$CONTENTS/Resources"
XCODE_SDK="/Applications/Xcode.app/Contents/Developer/Platforms/MacOSX.platform/Developer/SDKs/MacOSX.sdk"

if [[ -d "$XCODE_SDK" ]]; then
    export SDKROOT="$XCODE_SDK"
fi

echo "==> Running engine self-checks"
/usr/bin/python3 "$ROOT/engine/test_keyswitcher.py"
/usr/bin/python3 "$ROOT/engine/test_antigravity.py"

mkdir -p "$MACOS_DIR" "$RESOURCES_DIR"

ARCH="$(uname -m)"
case "$ARCH" in
    x86_64) TARGET="x86_64-apple-macos13" ;;
    arm64|aarch64) TARGET="arm64-apple-macos13" ;;
    *) TARGET="arm64-apple-macos13" ;;
esac

echo "==> Compiling main.swift ($TARGET)"
swiftc -O -swift-version 5 -target "$TARGET" \
    -o "$MACOS_DIR/KeySwitcher" \
    "$ROOT/app/main.swift" \
    "$ROOT/app/AntigravityView.swift" \
    "$ROOT/app/Localization.swift"

echo "==> Compiling Antigravity Keychain helper ($TARGET)"
# Sign with a stable Apple Development identity + fixed identifier so macOS
# Keychain "Always Allow" survives rebuilds. Ad-hoc signatures change CDHash
# every compile and force the password prompt again.
HELPER_BIN="$RESOURCES_DIR/antigravity-keychain"
HELPER_ID="com.eugene.keyswitcher.antigravity-keychain"
APP_SIGN_ID="com.eugene.keyswitcher"
if [[ -n "${KEYSWITCHER_CODESIGN_IDENTITY:-}" ]]; then
    CODESIGN_IDENTITY="$KEYSWITCHER_CODESIGN_IDENTITY"
else
    CODESIGN_IDENTITY="$(
        security find-identity -v -p codesigning 2>/dev/null \
            | /usr/bin/python3 -c 'import re,sys
for line in sys.stdin:
    if "CSSMERR" in line: continue
    m=re.search(r"\"(Apple Development: [^\"]+)\"", line)
    if m:
        print(m.group(1)); break'
    )"
fi
if [[ ! -x "$HELPER_BIN" || "$ROOT/app/antigravity_keychain.swift" -nt "$HELPER_BIN" ]]; then
    swiftc -O -swift-version 5 -target "$TARGET" \
        -framework Security \
        -o "$HELPER_BIN" \
        "$ROOT/app/antigravity_keychain.swift"
else
    echo "==> Reusing unchanged Antigravity Keychain helper binary"
fi
if [[ -n "${CODESIGN_IDENTITY:-}" ]]; then
    echo "==> Codesigning helper with: $CODESIGN_IDENTITY"
    codesign --force --sign "$CODESIGN_IDENTITY" --identifier "$HELPER_ID" "$HELPER_BIN"
else
    echo "WARNING: no Apple Development identity — helper stays ad-hoc (Keychain will keep prompting)" >&2
    codesign --force -s - --identifier "$HELPER_ID" "$HELPER_BIN"
fi

echo "==> Copying Info.plist"
cp "$ROOT/app/Info.plist" "$CONTENTS/Info.plist"

if [[ -f "$ROOT/app/KeySwitcher.icns" ]]; then
    echo "==> Copying app icon"
    cp "$ROOT/app/KeySwitcher.icns" "$RESOURCES_DIR/KeySwitcher.icns"
else
    echo "WARNING: $ROOT/app/KeySwitcher.icns not found — app will use the default icon" >&2
fi

if [[ -f "$ROOT/engine/keyswitcher.py" ]]; then
    echo "==> Bundling engine/keyswitcher.py into Resources"
    cp "$ROOT/engine/keyswitcher.py" "$RESOURCES_DIR/keyswitcher.py"
else
    echo "WARNING: $ROOT/engine/keyswitcher.py not found — the bundle will ship without the engine and the app won't find it (the engine is no longer read from ~/.codex)" >&2
fi

if [[ -f "$ROOT/engine/antigravity.py" ]]; then
    echo "==> Bundling engine/antigravity.py into Resources"
    cp "$ROOT/engine/antigravity.py" "$RESOURCES_DIR/antigravity.py"
else
    echo "WARNING: $ROOT/engine/antigravity.py not found — Antigravity switching will be unavailable" >&2
fi

if [[ -f "$ROOT/engine/antigravity_quota.py" ]]; then
    echo "==> Bundling engine/antigravity_quota.py into Resources"
    cp "$ROOT/engine/antigravity_quota.py" "$RESOURCES_DIR/antigravity_quota.py"
else
    echo "WARNING: $ROOT/engine/antigravity_quota.py not found — Antigravity quota lookup will be unavailable" >&2
fi

if [[ -f "$ROOT/engine/rotator.py" ]]; then
    echo "==> Bundling engine/rotator.py into Resources"
    cp "$ROOT/engine/rotator.py" "$RESOURCES_DIR/rotator.py"
else
    echo "WARNING: $ROOT/engine/rotator.py not found — switching daemon will be unavailable from bundled Resources" >&2
fi

printf 'APPL????' > "$CONTENTS/PkgInfo"

if [[ -n "${CODESIGN_IDENTITY:-}" ]]; then
    echo "==> Codesigning app with: $CODESIGN_IDENTITY"
    codesign --force --sign "$CODESIGN_IDENTITY" --identifier "$APP_SIGN_ID" "$APP_DIR"
else
    echo "==> Codesigning app (ad-hoc)"
    codesign --force -s - --identifier "$APP_SIGN_ID" "$APP_DIR"
fi

touch "$ROOT/dist/.metadata_never_index"
chflags hidden "$ROOT/dist" 2>/dev/null || true
chflags hidden "$APP_DIR" 2>/dev/null || true

echo "==> Built: $APP_DIR"
