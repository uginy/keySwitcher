# KeySwitcher

<p align="center">
  <b>A lightweight, native macOS menu bar switcher and live quota monitor for OpenAI Codex and Google Antigravity (CLI & IDE).</b>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/platform-macOS%2013%2B-black?style=flat-square&logo=apple" alt="macOS 13+">
  <img src="https://img.shields.io/badge/Swift-5.9%20%7C%20SwiftUI-orange?style=flat-square&logo=swift" alt="SwiftUI">
  <img src="https://img.shields.io/badge/Python-3.9%2B%20(stdlib%20only)-blue?style=flat-square&logo=python" alt="Python 3">
  <img src="https://img.shields.io/badge/Dependencies-0%20external-brightgreen?style=flat-square" alt="Zero Dependencies">
  <img src="https://img.shields.io/badge/license-MIT-green?style=flat-square" alt="MIT License">
</p>

---

## ✨ Features

- **⚡ 1-Click Instant Switching**:
  - **Codex**: Swaps token slots, safely persists current token state, performs a graceful restart, and automatically reopens your active conversation thread (`codex://threads/<id>`).
  - **Antigravity**: Simultaneously synchronizes authentication for both **Antigravity CLI** (macOS Keychain) and **Antigravity IDE** (SQLite storage) with a single click.

- **📊 Live Menu Bar Quota Monitor**:
  - Real-time remaining percentage and color-coded status badges directly in the macOS menu bar.
  - **Codex**: 5-hour window (`⏳`) & Weekly window (`🗓️`).
  - **Antigravity**: Independent breakdowns for **Gemini** (`✦`) and **Claude / GPT** (`⚡`) 5-hour & Weekly quotas.

- **🖥️ Clean Dual Dashboard**:
  - Compact side-by-side view showing both Codex and Antigravity accounts simultaneously without annoying tab toggling.
  - Displays plan badges (`Plus`, `Pro`, `Ultra`), visual usage bars, and exact reset countdown timers (e.g. `refresh in 1h 40m (23:54)`).

- **🔒 Privacy-First & 100% Local**:
  - Zero telemetry, zero external tracking servers.
  - Tokens are stored strictly in local macOS Keychain items and `chmod 600` files.
  - Tap-to-reveal email masking protects your accounts during screencasts and presentations.

- **🪶 Zero External Dependencies**:
  - Pure native Swift / SwiftUI / AppKit application.
  - Backend engine runs on standard macOS Python 3 (`/usr/bin/python3`) using only the standard library — **no `pip`, `venv`, or `node_modules` required**.

- **🖐️ Drag & Drop Reordering**:
  - Seamlessly reorder your account list by dragging handle bars, with instant tactile feedback and saved ordering.

- **🔄 Isolated Login & Re-authorization**:
  - Add new accounts or re-authorize expired sessions inside an isolated browser session without interrupting your current workflow.

---

## 🚀 Quick Start

### Installation

Clone the repository and run the automated installer:

```bash
git clone https://github.com/uginy/keySwitcher.git
cd keySwitcher
./install.sh
```

The script compiles the native binary, bundles the engine into `KeySwitcher.app`, signs it with your local development identity, installs it to `/Applications`, and starts the menu bar item.

### Updating

To pull the latest changes and rebuild:

```bash
git pull
./update.sh
```

### Uninstallation

To cleanly remove the application and runtime metadata:

```bash
./uninstall.sh
```
*(Your token slots in `~/.codex/accounts/` and Keychain items are preserved by default).*

---

## 🏗️ Architecture

```
[ macOS Menu Bar ]
       │  (AppKit + SwiftUI Frontend)
       ▼
 KeySwitcher.app
       │
       ├──► OpenAI Codex
       │       ├─► Slots:  ~/.codex/accounts/auth_*.json (chmod 600)
       │       ├─► Active: ~/.codex/auth.json
       │       └─► API:    chatgpt.com/backend-api/wham/usage
       │
       └──► Google Antigravity
               ├─► CLI:    macOS Keychain (service=gemini)
               ├─► IDE:    ~/Library/Application Support/Antigravity/User/globalStorage/state.vscdb
               └─► API:    cloudcode-pa.googleapis.com (loadCodeAssist)
```

---

## ⌨️ CLI Usage

The core engine can also be operated directly from the terminal or integrated into custom shell scripts:

### Codex Engine (`engine/keyswitcher.py`)

```bash
# Check status and quota of all Codex accounts
python3 engine/keyswitcher.py status

# Switch to slot 2 (graceful restart + restore active thread)
python3 engine/keyswitcher.py switch 2

# Add a new account via isolated browser login
python3 engine/keyswitcher.py add

# Re-authenticate an expired slot without switching
python3 engine/keyswitcher.py relogin 2

# Remove slot 2
python3 engine/keyswitcher.py delete 2
```

### Antigravity Engine (`engine/antigravity.py`)

```bash
# Check status, accounts, and quota breakdown
python3 engine/antigravity.py status

# Switch account across CLI and IDE (all targets)
python3 engine/antigravity.py switch <profile_id> all

# Switch account for CLI only or IDE only
python3 engine/antigravity.py switch <profile_id> cli
python3 engine/antigravity.py switch <profile_id> ide

# Start OAuth login for a new account
python3 engine/antigravity.py begin-login cli
```

---

## 🛠️ Requirements

- **macOS**: 13.0 (Ventura) or later (Apple Silicon M1-M4 & Intel x86_64).
- **Xcode Command Line Tools**: `xcode-select --install` (for `swiftc`).
- **Python**: 3.9+ (pre-installed on macOS).
- **Optional**: Accessibility permissions for KeySwitcher (`System Settings -> Privacy & Security -> Accessibility`) to enable smooth Codex window reload (`Cmd+R`).

---

## 📄 License

This project is licensed under the MIT License — see the [LICENSE](LICENSE) file for details.
