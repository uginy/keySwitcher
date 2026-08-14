#!/usr/bin/env python3
"""Codex account rotation and reactive log daemon for KeySwitcher.

This module is intentionally standalone and stdlib-only. KeySwitcher imports
`rotate()` for manual/proactive switches, and LaunchAgent runs this file with
the `daemon` command for reactive rotation on 429/401 log events.
"""

import base64
import contextlib
import fcntl
import json
import logging
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

CODEX_DIR = Path.home() / ".codex"
ACCOUNTS_DIR = CODEX_DIR / "accounts"
AUTH_FILE = CODEX_DIR / "auth.json"
CODEX_STATE_DB = CODEX_DIR / "state_5.sqlite"
# Shared with keyswitcher.py so manual switch and the LaunchAgent daemon
# serialize auth/slot file mutations. Path must stay identical.
LOCK_FILE = CODEX_DIR / "keyswitcher" / ".lock"
DAEMON_LOG = Path.home() / "Library" / "Logs" / "KeySwitcher" / "daemon.log"
CODEX_LOG_DIR = Path.home() / "Library" / "Logs" / "com.openai.codex"

LIMIT_KEYWORDS = [
    "rate_limit_exceeded",
    "too_many_requests",
    '"status": 429',
    '"status":429',
    "reached the limit for gpt",
    "reached your limit",
    "resourceexhausted",
    "token_revoked",
    "token_invalidated",
    "invalidated oauth token",
    "authentication token has been invalidated",
    "logged out or signed in",
    "could not be refreshed",
    '"status": 401',
    '"status":401',
]

@contextlib.contextmanager
def exclusive_lock():
    """Serialize auth/slot file writes with keyswitcher.py.

    Hold only around filesystem copies — never around Codex quit/relaunch.
    """
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    fh = open(LOCK_FILE, "a")
    try:
        with contextlib.suppress(OSError):
            os.chmod(LOCK_FILE, 0o600)
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        fh.close()


def read_json(path):
    try:
        with open(path, "r") as fh:
            return json.load(fh)
    except Exception:
        return None


def token_payload(token):
    try:
        part = token.split(".")[1]
        part += "=" * (-len(part) % 4)
        return json.loads(base64.urlsafe_b64decode(part.encode("ascii")))
    except Exception:
        return {}


def email_from_payload(payload):
    return payload.get("email") or (payload.get("https://api.openai.com/profile") or {}).get("email")


def get_email_from_token(token):
    return email_from_payload(token_payload(token)) if token else None


def get_account_email_from_file(path):
    data = read_json(path) or {}
    tokens = data.get("tokens") or {}
    return (
        get_email_from_token(tokens.get("id_token"))
        or get_email_from_token(tokens.get("access_token"))
        or "Unknown Email"
    )


def slot_number_from_path(path):
    try:
        return int(path.stem.split("_", 1)[1])
    except (IndexError, ValueError):
        return None


def account_slots():
    if not ACCOUNTS_DIR.is_dir():
        return []
    slots = []
    for path in ACCOUNTS_DIR.glob("auth_*.json"):
        number = slot_number_from_path(path)
        if number is not None:
            slots.append((number, path))
    return sorted(slots, key=lambda item: item[0])


def get_active_account_id():
    data = read_json(AUTH_FILE) or {}
    return (data.get("tokens") or {}).get("account_id")


def account_id_from_file(path):
    data = read_json(path) or {}
    return (data.get("tokens") or {}).get("account_id")


def sync_active_token_to_slot():
    if not AUTH_FILE.exists() or not ACCOUNTS_DIR.exists():
        return False
    # Hold the shared lock only for the read-compare-copy of auth files.
    with exclusive_lock():
        active_id = get_active_account_id()
        if not active_id:
            return False
        active_data = read_json(AUTH_FILE)
        if not isinstance(active_data, dict):
            return False
        for number, path in account_slots():
            data = read_json(path)
            if not isinstance(data, dict):
                continue
            if (data.get("tokens") or {}).get("account_id") != active_id:
                continue
            if data.get("tokens") != active_data.get("tokens"):
                shutil.copy2(AUTH_FILE, path)
                print("synced refreshed tokens for %s to slot %d" % (
                    get_account_email_from_file(path), number))
                return True
            return False
    return False


def codex_app_is_running():
    """True if a Codex desktop process appears to be alive.

    Process names vary; force-kill below still targets only `Codex`.
    """
    for name in ("Codex", "ChatGPT"):
        if subprocess.run(
            ["pgrep", "-x", name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        ).returncode == 0:
            return True
    return False


def quit_codex_app():
    if not codex_app_is_running():
        print("Codex app is not running; nothing to restart.")
        return
    print("Quitting Codex to apply the new account...")
    with contextlib.suppress(Exception):
        subprocess.run(
            ["osascript", "-e", 'tell application id "com.openai.codex" to quit'],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
    for _ in range(24):
        if not codex_app_is_running():
            print("Codex quit cleanly.")
            return
        time.sleep(0.25)
    # Never killall ChatGPT — that can take down an unrelated app.
    subprocess.run(
        ["killall", "Codex"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    time.sleep(0.5)
    print("Codex force-quit because it did not exit cleanly.")


def relaunch_codex_app():
    subprocess.run(["open", "-b", "com.openai.codex"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print("Codex relaunched with the new account.")


def latest_local_thread_id():
    """Best-effort Codex desktop thread to restore after the account switch."""
    if not CODEX_STATE_DB.exists():
        return None
    try:
        con = sqlite3.connect("file:%s?mode=ro" % CODEX_STATE_DB, uri=True, timeout=1)
        try:
            row = con.execute("""
                SELECT id
                FROM threads
                WHERE archived = 0
                ORDER BY updated_at_ms DESC, updated_at DESC
                LIMIT 1
            """).fetchone()
        finally:
            con.close()
    except sqlite3.Error:
        return None
    return row[0] if row and row[0] else None


def reopen_codex_thread(thread_id):
    if not thread_id:
        return False
    try:
        return subprocess.run(
            ["open", "codex://threads/%s" % thread_id],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
        ).returncode == 0
    except Exception:
        return False


def rotate(target_slot=None, restart_app=True):
    slots = account_slots()
    if not slots:
        print("No account slots found in %s." % ACCOUNTS_DIR)
        return False
    if len(slots) < 2 and target_slot is None:
        print("At least 2 account slots are required for rotation.")
        return False

    print("Found %d account slot(s):" % len(slots))
    for number, path in slots:
        print("  [%d] %s -> %s" % (number, path.name, get_account_email_from_file(path)))

    active_id = get_active_account_id()
    active = next(((number, path) for number, path in slots if account_id_from_file(path) == active_id), None)

    if target_slot is not None:
        target = next(((number, path) for number, path in slots if number == target_slot), None)
        if target is None:
            print("Invalid slot number: %s." % target_slot)
            return False
    elif active is None:
        print("No active account identified; switching to slot %d." % slots[0][0])
        target = slots[0]
    else:
        active_index = slots.index(active)
        target = slots[(active_index + 1) % len(slots)]

    target_number, target_path = target
    if active and active[0] == target_number:
        print("Slot %d is already active." % target_number)
        return True

    # File mutations only — quit/relaunch stays outside the lock so a slow
    # Codex restart cannot block keyswitcher status/refresh/add.
    with exclusive_lock():
        if active:
            active_number, active_path = active
            if AUTH_FILE.exists():
                shutil.copy2(AUTH_FILE, active_path)
                print("Saved refreshed tokens back to slot %d." % active_number)

        if AUTH_FILE.exists():
            shutil.copy2(AUTH_FILE, CODEX_DIR / "auth.json.bak")

        shutil.copy2(target_path, AUTH_FILE)
    print("Updated %s from slot %d." % (AUTH_FILE, target_number))
    if restart_app:
        restore_thread_id = latest_local_thread_id()
        quit_codex_app()
        relaunch_codex_app()
        if restore_thread_id and reopen_codex_thread(restore_thread_id):
            print("Reopened Codex thread %s." % restore_thread_id)
    else:
        print("Codex restart skipped by request.")
    print("Switched successfully to %s." % get_account_email_from_file(target_path))
    return True


def setup_logging():
    CODEX_DIR.mkdir(parents=True, exist_ok=True)
    DAEMON_LOG.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.FileHandler(DAEMON_LOG, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )


def find_latest_log_file():
    if not CODEX_LOG_DIR.exists():
        return None
    logs = list(CODEX_LOG_DIR.glob("**/codex-desktop-*-t0-*.log"))
    if not logs:
        logs = list(CODEX_LOG_DIR.glob("**/codex-desktop-*.log"))
    if not logs:
        return None
    logs.sort(key=lambda path: path.stat().st_mtime)
    return logs[-1]


def watch_log_file():
    setup_logging()
    current_file = find_latest_log_file()
    while not current_file:
        logging.info("Waiting for active Codex desktop log file...")
        time.sleep(5)
        current_file = find_latest_log_file()

    logging.info("Starting log monitor daemon. Watching: %s", current_file)
    logging.info("Logging daemon output to: %s", DAEMON_LOG)

    last_rotation_time = 0
    cooldown_seconds = 60
    recent_rotations = []
    last_file_check_time = time.time()

    file = open(current_file, "r", encoding="utf-8", errors="ignore")
    try:
        file.seek(0, os.SEEK_END)
        inode = os.fstat(file.fileno()).st_ino
        while True:
            now = time.time()
            if now - last_file_check_time > 10:
                last_file_check_time = now
                with contextlib.suppress(Exception):
                    if sync_active_token_to_slot():
                        logging.info("Synced newly refreshed active token back to its slot.")
                newest = find_latest_log_file()
                if newest and newest != current_file:
                    logging.info("Found newer active log file: %s", newest)
                    file.close()
                    current_file = newest
                    file = open(current_file, "r", encoding="utf-8", errors="ignore")
                    file.seek(0, os.SEEK_END)
                    inode = os.fstat(file.fileno()).st_ino

            if current_file.exists():
                stat = current_file.stat()
                if stat.st_ino != inode or stat.st_size < file.tell():
                    logging.info("Active log file rotated or truncated; reopening.")
                    file.close()
                    file = open(current_file, "r", encoding="utf-8", errors="ignore")
                    inode = os.fstat(file.fileno()).st_ino

            line = file.readline()
            if not line:
                time.sleep(1)
                continue

            if not any(keyword in line.lower() for keyword in LIMIT_KEYWORDS):
                continue
            if now - last_rotation_time < cooldown_seconds:
                logging.debug("Auth/rate error detected, but daemon is in cooldown.")
                continue

            slot_count = len(account_slots())
            recent_rotations[:] = [ts for ts in recent_rotations if now - ts < 600]
            if slot_count > 0 and len(recent_rotations) >= slot_count:
                logging.warning("All %d account slots rotated within 10 min; backing off for 30 min.", slot_count)
                last_rotation_time = now + 1800
                recent_rotations.clear()
                continue

            logging.warning("Trigger detected in logs: %s", line.strip())
            try:
                if rotate():
                    last_rotation_time = time.time()
                    recent_rotations.append(last_rotation_time)
                    logging.info("Automatic rotation completed successfully.")
            except Exception as exc:
                logging.error("Automatic rotation failed: %s", exc)
    finally:
        file.close()


def setup():
    ACCOUNTS_DIR.mkdir(parents=True, exist_ok=True)
    print("Accounts directory: %s" % ACCOUNTS_DIR)
    if AUTH_FILE.exists() and not (ACCOUNTS_DIR / "auth_1.json").exists():
        shutil.copy2(AUTH_FILE, ACCOUNTS_DIR / "auth_1.json")
        print("Saved current active account as auth_1.json.")


def main(argv):
    command = argv[1] if len(argv) > 1 else ""
    if command == "daemon":
        watch_log_file()
        return 0
    if command == "setup":
        setup()
        return 0
    if command:
        try:
            return 0 if rotate(int(command)) else 1
        except ValueError:
            print("Unknown argument: %s. Use a slot number, setup, or daemon." % command)
            return 2
    return 0 if rotate() else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
