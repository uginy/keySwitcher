#!/usr/bin/env python3
"""Minimal Antigravity account snapshots for KeySwitcher.

The Antigravity app and CLI share one OS credential store (macOS Keychain or
Windows Credential Manager). IDE auth is restored from its state.vscdb.
Saved snapshots stay in the credential store and token values are never
returned in command JSON.
"""

import base64
import concurrent.futures
import contextlib
from datetime import datetime
import hashlib
import http.server
import json
import os
import re
import secrets
import signal
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path

import antigravity_quota
import compat

HOME = Path.home()
ROOT = Path(os.environ.get(
    "KEYSWITCHER_ANTIGRAVITY_ROOT",
    compat.antigravity_default_root(),
)).expanduser()
STATE_FILE = ROOT / "profiles.json"
LOCK_FILE = ROOT / ".lock"
BACKUP_DIR = ROOT / "backups"

DB_PATHS = {
    "ide": Path(os.environ.get(
        "KEYSWITCHER_ANTIGRAVITY_IDE_DB",
        compat.antigravity_ide_db(),
    )).expanduser(),
}
BUNDLE_IDS = {
    "ide": "com.google.antigravity-ide",
}
APP_PATHS = {
    "ide": Path(os.environ.get(
        "KEYSWITCHER_ANTIGRAVITY_IDE_APP",
        compat.antigravity_ide_app(),
    )).expanduser(),
}
SHARED_APP_BUNDLE_ID = "com.google.antigravity"
SHARED_APP_PATH = Path(os.environ.get(
    "KEYSWITCHER_ANTIGRAVITY_APP",
    compat.antigravity_shared_app(),
)).expanduser()

PROFILE_SERVICE = "com.eugene.keyswitcher.antigravity"
SNAPSHOT_ACCOUNT = "vault"
CLI_SERVICE = "gemini"
CLI_ACCOUNT = "antigravity"
CLI_CREDENTIALS_FILE = Path(os.environ.get(
    "KEYSWITCHER_ANTIGRAVITY_CLI_FILE",
    HOME / ".gemini/oauth_creds.json",
)).expanduser()
GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v2/userinfo"
TARGETS = {"cli", "ide"}
LIMIT_EXHAUSTED_PERCENT = 99.0
IDE_OAUTH_SCOPES = (
    "https://www.googleapis.com/auth/cloud-platform",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
    "https://www.googleapis.com/auth/cclog",
    "https://www.googleapis.com/auth/experimentsandconfigs",
)

AUTH_KEYS = (
    "antigravityUnifiedStateSync.oauthToken",
    "antigravityUnifiedStateSync.userStatus",
    "antigravityUnifiedStateSync.enterprisePreferences",
    "antigravityAuthStatus",
    "jetskiStateSync.agentManagerInitState",
)
EMAIL_RE = re.compile(rb"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
BASE64_RE = re.compile(rb"[A-Za-z0-9+/]{24,}={0,2}")


def write_json_atomic(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix="." + path.name + ".")
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(data, handle, indent=2)
        compat.secure_chmod(temporary, 0o600)
        os.replace(temporary, path)
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise


@contextlib.contextmanager
def exclusive_lock():
    with compat.exclusive_lock(LOCK_FILE):
        yield


def load_state():
    try:
        data = json.loads(STATE_FILE.read_text())
    except Exception:
        data = {}
    profiles = data.get("profiles") if isinstance(data.get("profiles"), list) else []
    legacy_agent_ids = [
        profile.get("id") for profile in profiles
        if isinstance(profile, dict) and "agent" in profile.get("targets", [])
    ]
    profiles = [
        {**profile, "targets": [target for target in profile.get("targets", []) if target in TARGETS]}
        for profile in profiles
        if isinstance(profile, dict)
    ]
    profiles = [profile for profile in profiles if profile["targets"]]
    raw_order = data.get("order") if isinstance(data.get("order"), dict) else {}
    order = {}
    for target in TARGETS:
        available = [profile["id"] for profile in profiles if target in profile["targets"]]
        saved = raw_order.get(target) if isinstance(raw_order.get(target), list) else []
        saved = [profile_id for profile_id in saved if profile_id in available]
        order[target] = list(dict.fromkeys(saved + available))
    state = {
        "profiles": profiles,
        "order": order,
        "active": {
            target: profile_id for target, profile_id in data.get("active", {}).items()
            if target in TARGETS
        } if isinstance(data.get("active"), dict) else {},
        "pending": {
            target: pending for target, pending in data.get("pending", {}).items()
            if target in TARGETS
        } if isinstance(data.get("pending"), dict) else {},
        "autoswitch": {
            target: enabled is True for target, enabled in data.get("autoswitch", {}).items()
            if target in TARGETS
        } if isinstance(data.get("autoswitch"), dict) else {},
    }
    if legacy_agent_ids:
        for profile_id in filter(None, legacy_agent_ids):
            with contextlib.suppress(Exception):
                run_keychain(
                    "delete", PROFILE_SERVICE, profile_account(profile_id, "agent"),
                    allow_missing=True,
                )
        write_json_atomic(STATE_FILE, state)
    return state


def keychain_helper_path():
    configured = os.environ.get("KEYSWITCHER_ANTIGRAVITY_KEYCHAIN_HELPER")
    candidates = [
        Path(configured).expanduser() if configured else None,
        Path(__file__).with_name("antigravity-keychain"),
        Path(__file__).parent.parent / "dist/KeySwitcher.app/Contents/Resources/antigravity-keychain",
    ]
    for candidate in candidates:
        if candidate and candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    raise RuntimeError("Antigravity Keychain helper not found")


def run_keychain(command, service, account, value=None, allow_missing=False):
    helper = os.environ.get("KEYSWITCHER_ANTIGRAVITY_KEYCHAIN_HELPER")
    use_helper = bool(helper)
    if not use_helper:
        try:
            keychain_helper_path()
            use_helper = True
        except RuntimeError:
            use_helper = False

    if not use_helper and compat.IS_WIN:
        try:
            if command == "get":
                blob = compat.wincred_get(service, account)
                if blob is None:
                    if allow_missing:
                        return None
                    raise RuntimeError("Keychain operation failed (44)")
                return blob
            if command == "set":
                compat.wincred_set(service, account, value or b"")
                return b""
            if command == "delete":
                compat.wincred_delete(service, account)
                return b""
        except RuntimeError:
            raise
        except Exception as exc:
            raise RuntimeError("Keychain operation failed (%s)" % exc) from exc
        raise RuntimeError("Unknown credential command: %s" % command)

    result = subprocess.run(
        compat.helper_command(keychain_helper_path()) + [command, service, account],
        input=value,
        capture_output=True,
        timeout=30,
    )
    if result.returncode == 44 and allow_missing:
        return None
    if result.returncode != 0:
        raise RuntimeError("Keychain operation failed (%d)" % result.returncode)
    return result.stdout


def profile_account(profile_id, target):
    return "%s:%s" % (profile_id, target)


def load_snapshot_vault():
    raw = run_keychain("get", PROFILE_SERVICE, SNAPSHOT_ACCOUNT, allow_missing=True)
    if raw is None:
        return {}
    try:
        vault = json.loads(raw.decode())
    except Exception as exc:
        raise RuntimeError("Saved profile vault is unreadable") from exc
    if not isinstance(vault, dict):
        raise RuntimeError("Saved profile vault is invalid")
    return vault


def write_snapshot_vault(vault):
    if vault:
        run_keychain(
            "set", PROFILE_SERVICE, SNAPSHOT_ACCOUNT,
            json.dumps(vault, separators=(",", ":")).encode(),
        )
    else:
        run_keychain("delete", PROFILE_SERVICE, SNAPSHOT_ACCOUNT, allow_missing=True)


def save_vault_snapshot(profile_id, target, snapshot):
    vault = load_snapshot_vault()
    vault[profile_account(profile_id, target)] = snapshot
    write_snapshot_vault(vault)


def delete_vault_snapshot(profile_id, target):
    vault = load_snapshot_vault()
    key = profile_account(profile_id, target)
    if key in vault:
        del vault[key]
        write_snapshot_vault(vault)


def profile_id_for_email(email):
    return hashlib.sha256(email.strip().lower().encode()).hexdigest()[:16]


def protobuf_varint(value):
    result = bytearray()
    while value >= 0x80:
        result.append((value & 0x7f) | 0x80)
        value >>= 7
    result.append(value)
    return bytes(result)


def protobuf_bytes(field, value):
    return protobuf_varint((field << 3) | 2) + protobuf_varint(len(value)) + value


def encode_ide_oauth_state(credentials):
    oauth = b"".join((
        protobuf_bytes(1, credentials["access_token"].encode()),
        protobuf_bytes(2, credentials.get("token_type", "Bearer").encode()),
        protobuf_bytes(3, credentials["refresh_token"].encode()),
    ))
    row = protobuf_bytes(1, base64.b64encode(oauth))
    entry = protobuf_bytes(1, b"oauthTokenInfoSentinelKey") + protobuf_bytes(2, row)
    return base64.b64encode(protobuf_bytes(1, entry)).decode()


def save_snapshot(email, target, snapshot, activate=True):
    profile_id = profile_id_for_email(email)
    save_vault_snapshot(profile_id, target, snapshot)

    state = load_state()
    profile = next((item for item in state["profiles"] if item.get("id") == profile_id), None)
    if profile is None:
        profile = {"id": profile_id, "email": email, "targets": []}
        state["profiles"].append(profile)
    profile["email"] = email
    profile["targets"] = sorted(set(profile.get("targets", [])) | {target})
    profile["updated_at"] = int(time.time())
    if activate:
        state["active"][target] = profile_id
    if profile_id not in state["order"][target]:
        state["order"][target].append(profile_id)
    state["profiles"].sort(key=lambda item: item.get("email", "").lower())
    write_json_atomic(STATE_FILE, state)
    return profile


def load_snapshot(profile_id, target):
    key = profile_account(profile_id, target)
    vault = load_snapshot_vault()
    snapshot = vault.get(key)
    if snapshot is not None:
        return snapshot

    raw = run_keychain("get", PROFILE_SERVICE, key, allow_missing=True)
    if raw is None:
        raise RuntimeError("Profile has no %s credentials" % target)
    try:
        snapshot = json.loads(raw.decode())
    except Exception as exc:
        raise RuntimeError("Saved profile is unreadable") from exc
    save_vault_snapshot(profile_id, target, snapshot)
    run_keychain("delete", PROFILE_SERVICE, key, allow_missing=True)
    return snapshot


def nested_emails(value):
    queue = [(str(value).encode(), 0)]
    seen = set()
    emails = set()
    while queue:
        blob, depth = queue.pop(0)
        digest = hashlib.sha256(blob).digest()
        if digest in seen or len(blob) > 1_000_000:
            continue
        seen.add(digest)
        emails.update(match.decode("ascii") for match in EMAIL_RE.findall(blob))
        if depth >= 4:
            continue
        for candidate in BASE64_RE.findall(blob):
            # Protobuf length bytes may be valid base64 characters and become
            # attached to an embedded base64 field. Try the four alignments.
            for offset in range(min(4, len(candidate))):
                aligned = candidate[offset:]
                if len(aligned) < 24:
                    continue
                try:
                    decoded = base64.b64decode(aligned + b"=" * (-len(aligned) % 4))
                except Exception:
                    continue
                if decoded and decoded != aligned:
                    queue.append((decoded, depth + 1))
    return sorted(emails)


def email_from_rows(rows):
    auth_status = rows.get("antigravityAuthStatus")
    if auth_status:
        try:
            email = json.loads(auth_status).get("email")
            if email:
                return email
        except Exception:
            pass
    emails = set()
    for value in rows.values():
        emails.update(nested_emails(value))
    if not emails:
        raise RuntimeError("Could not identify the signed-in Antigravity account")
    return sorted(emails)[0]


def read_gui_state(target):
    path = DB_PATHS[target]
    if not path.is_file():
        raise RuntimeError("%s state.vscdb not found" % target)
    uri = "file:%s?mode=ro" % path.as_posix()
    connection = sqlite3.connect(uri, uri=True, timeout=3)
    try:
        placeholders = ",".join("?" for _ in AUTH_KEYS)
        rows = dict(connection.execute(
            "SELECT key, value FROM ItemTable WHERE key IN (%s)" % placeholders,
            AUTH_KEYS,
        ).fetchall())
    finally:
        connection.close()
    if (
        "antigravityUnifiedStateSync.oauthToken" not in rows
        and "jetskiStateSync.agentManagerInitState" not in rows
    ):
        raise RuntimeError("No signed-in account found in %s" % target)
    return {"kind": "sqlite", "rows": rows}


def decode_cli_payload(raw):
    text = raw.decode().strip()
    if text.startswith("go-keyring-base64:"):
        text = base64.b64decode(text.split(":", 1)[1]).decode()
    payload = json.loads(text)
    token = payload.get("token") or {}
    if not token.get("access_token") or not token.get("refresh_token"):
        raise RuntimeError("Antigravity CLI credential is incomplete")
    return payload


def write_cli_credentials_file(snapshot):
    credentials = snapshot_credentials(snapshot)
    existing = {}
    if CLI_CREDENTIALS_FILE.is_file():
        with contextlib.suppress(OSError, ValueError):
            existing = json.loads(CLI_CREDENTIALS_FILE.read_text())
    if not isinstance(existing, dict):
        existing = {}

    payload = {
        "access_token": credentials["access_token"],
        "refresh_token": credentials["refresh_token"],
        "scope": existing.get("scope") or " ".join((*IDE_OAUTH_SCOPES, "openid")),
        "token_type": credentials.get("token_type", "Bearer"),
        "expiry_date": 0,
    }
    if credentials.get("id_token"):
        payload["id_token"] = credentials["id_token"]
    expiry = credentials.get("expiry")
    if expiry:
        try:
            payload["expiry_date"] = int(datetime.fromisoformat(
                expiry.replace("Z", "+00:00")
            ).timestamp() * 1000)
        except ValueError:
            pass
    write_json_atomic(CLI_CREDENTIALS_FILE, payload)


def cli_email_from_log():
    log_dir = HOME / ".gemini/antigravity-cli/log"
    pattern = re.compile(r"applyAuthResult: email=([^,\s]+)")
    for path in sorted(log_dir.glob("*.log"), reverse=True):
        try:
            matches = pattern.findall(path.read_text(errors="ignore"))
        except OSError:
            continue
        if matches:
            return matches[-1]
    return None


def cli_email(payload):
    try:
        return google_email(payload["token"])
    except Exception:
        pass
    if payload.get("email"):
        return payload["email"]
    email = cli_email_from_log()
    if email:
        return email
    raise RuntimeError("Could not identify the signed-in Antigravity CLI account")


def capture(target):
    if target in DB_PATHS:
        email, snapshot = read_gui_snapshot(target)
    elif target == "cli":
        raw = run_keychain("get", CLI_SERVICE, CLI_ACCOUNT)
        payload = decode_cli_payload(raw)
        email = cli_email(payload)
        snapshot = {"kind": "keychain", "payload": raw.decode()}
    else:
        raise RuntimeError("Unknown Antigravity target: %s" % target)
    profile = save_snapshot(email, target, snapshot)
    return {"ok": True, "profile": profile, "target": target}


def read_current_snapshot(target):
    if target in DB_PATHS:
        return read_gui_state(target)
    if target == "cli":
        raw = run_keychain("get", CLI_SERVICE, CLI_ACCOUNT)
        decode_cli_payload(raw)
        return {"kind": "keychain", "payload": raw.decode()}
    raise RuntimeError("Unknown Antigravity target: %s" % target)


def snapshot_credentials(snapshot):
    if snapshot.get("kind") == "sqlite":
        return antigravity_quota.extract_gui_credentials_from_rows(snapshot.get("rows") or {})
    if snapshot.get("kind") == "keychain":
        return antigravity_quota.extract_cli_credentials(snapshot.get("payload", ""))
    raise RuntimeError("Saved Antigravity credential is invalid")


def refresh_ide_snapshot(snapshot):
    credentials = snapshot_credentials(snapshot)
    credentials["access_token"] = antigravity_quota.refresh_access_token(
        credentials["refresh_token"], compat.antigravity_user_agent(),
    )
    rows = dict(snapshot["rows"])
    rows["antigravityUnifiedStateSync.oauthToken"] = encode_ide_oauth_state(credentials)
    return {**snapshot, "rows": rows}


def snapshot_digest(snapshot):
    credentials = snapshot_credentials(snapshot)
    material = "%s\0%s" % (
        credentials.get("access_token", ""),
        credentials.get("refresh_token", ""),
    )
    return hashlib.sha256(material.encode()).hexdigest()


def google_email(credentials):
    for key in ("id_token", "access_token"):
        token = credentials.get(key)
        try:
            payload = token.split(".")[1]
            payload += "=" * (-len(payload) % 4)
            email = json.loads(base64.urlsafe_b64decode(payload)).get("email")
            if email:
                return email
        except Exception:
            pass
    access_token = credentials["access_token"]
    for attempt in range(2):
        request = urllib.request.Request(
            GOOGLE_USERINFO_URL,
            headers={"Authorization": "Bearer " + access_token, "User-Agent": "KeySwitcher/1"},
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                email = json.loads(response.read().decode()).get("email")
                if email:
                    return email
                raise RuntimeError("Google userinfo has no email")
        except Exception:
            if attempt == 0:
                access_token = antigravity_quota.refresh_access_token(
                    credentials["refresh_token"], compat.antigravity_user_agent(),
                )
                continue
            raise
    raise RuntimeError("Could not identify the Antigravity account")


def exchange_oauth_code(code, redirect_uri):
    client_id, client_secret = antigravity_quota.official_oauth_client()
    fields = {
        "client_id": client_id,
        "code": code,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
    }
    if client_secret:
        fields["client_secret"] = client_secret
    request = urllib.request.Request(
        antigravity_quota.TOKEN_ENDPOINT,
        data=urllib.parse.urlencode(fields).encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        credentials = json.loads(response.read().decode())
    if not credentials.get("access_token") or not credentials.get("refresh_token"):
        raise RuntimeError("Google OAuth did not return reusable credentials")
    return credentials


def run_oauth_worker(target, result_path):
    result_path = Path(result_path)
    expected_state = secrets.token_urlsafe(32)
    callback = {}

    class CallbackHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            query = urllib.parse.parse_qs(parsed.query)
            if parsed.path != "/oauth-callback" or query.get("state", [None])[0] != expected_state:
                self.send_response(400)
                self.end_headers()
                return
            callback["code"] = query.get("code", [None])[0]
            callback["error"] = query.get("error", [None])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(
                (
                    "<html><body><h2>Antigravity %s account received.</h2>"
                    "<p>You can close this tab.</p></body></html>" % target.upper()
                ).encode()
            )

        def log_message(self, _format, *_args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), CallbackHandler)
    server.timeout = 1
    redirect_uri = "http://localhost:%d/oauth-callback" % server.server_port
    client_id, _ = antigravity_quota.official_oauth_client()
    query = urllib.parse.urlencode({
        "access_type": "offline",
        "scope": " ".join(IDE_OAUTH_SCOPES),
        "state": expected_state,
        "prompt": "consent select_account",
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
    })
    try:
        if not compat.open_url("https://accounts.google.com/o/oauth2/v2/auth?" + query):
            raise RuntimeError("Could not open the browser for Google OAuth")
        deadline = time.monotonic() + 300
        while not callback and time.monotonic() < deadline:
            server.handle_request()
        if callback.get("error"):
            raise RuntimeError("Google OAuth was cancelled")
        if not callback.get("code"):
            raise RuntimeError("Google OAuth timed out")
        credentials = exchange_oauth_code(callback["code"], redirect_uri)
        write_json_atomic(result_path, {
            "ok": True,
            "email": google_email(credentials),
            "credentials": credentials,
        })
    except Exception as exc:
        write_json_atomic(result_path, {"ok": False, "error": str(exc)})
    finally:
        server.server_close()


def encode_cli_oauth_payload(email, credentials):
    token = {
        key: credentials[key]
        for key in ("access_token", "refresh_token", "token_type", "id_token")
        if credentials.get(key)
    }
    expires_in = credentials.get("expires_in")
    if expires_in:
        token["expiry"] = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ",
            time.gmtime(time.time() + int(expires_in)),
        )
    return json.dumps({
        "email": email,
        "auth_method": "consumer",
        "token": token,
    }, separators=(",", ":"))


def process_is_running(pid):
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ValueError):
        return False


def read_gui_snapshot(target):
    snapshot = read_gui_state(target)
    try:
        email = google_email(snapshot_credentials(snapshot))
    except Exception:
        email = email_from_rows(snapshot["rows"])
    return email, snapshot


def app_is_running(bundle_id):
    return compat.antigravity_app_running(bundle_id)


def stop_app(bundle_id):
    compat.stop_antigravity_app(bundle_id)


def open_app(bundle_id):
    exe = None
    if bundle_id == BUNDLE_IDS.get("ide"):
        exe = APP_PATHS["ide"] / "Antigravity.exe" if compat.IS_WIN else None
    elif compat.IS_WIN:
        exe = SHARED_APP_PATH / "Antigravity.exe"
    compat.open_antigravity_app(bundle_id, exe)


def find_cli_binary():
    candidates = [
        os.environ.get("KEYSWITCHER_ANTIGRAVITY_CLI"),
        shutil.which("agy"),
        shutil.which("agy.cmd") if compat.IS_WIN else None,
        str(HOME / ".local/bin/agy"),
        "/opt/homebrew/bin/agy",
        "/usr/local/bin/agy",
    ]
    if compat.IS_WIN:
        candidates.extend([
            str(compat.antigravity_ide_app() / "bin" / "agy.exe"),
            str(compat.antigravity_ide_app() / "bin" / "agy.cmd"),
        ])
    return next((path for path in candidates if path and Path(path).is_file()), None)


def backup_database(source, target):
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    destination = BACKUP_DIR / (target + ".state.vscdb")
    source_connection = sqlite3.connect(source, timeout=3)
    destination_connection = sqlite3.connect(destination)
    try:
        source_connection.backup(destination_connection)
    finally:
        destination_connection.close()
        source_connection.close()
    compat.secure_chmod(destination, 0o600)


def clear_gui_auth(target):
    path = DB_PATHS[target]
    if not path.is_file():
        raise RuntimeError("%s state.vscdb not found" % target)
    bundle_id = BUNDLE_IDS[target]
    if app_is_running(bundle_id):
        stop_app(bundle_id)
    backup_database(path, target)
    connection = sqlite3.connect(path, timeout=5)
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.executemany("DELETE FROM ItemTable WHERE key = ?", ((key,) for key in AUTH_KEYS))
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    open_app(bundle_id)


def switch_gui(profile_id, target, snapshot):
    if snapshot.get("kind") != "sqlite" or not isinstance(snapshot.get("rows"), dict):
        raise RuntimeError("Saved GUI snapshot is invalid")
    if os.environ.get("KEYSWITCHER_ANTIGRAVITY_SKIP_TOKEN_REFRESH") != "1":
        snapshot = refresh_ide_snapshot(snapshot)
    rows = {key: value for key, value in snapshot["rows"].items() if key in AUTH_KEYS}
    if (
        "antigravityUnifiedStateSync.oauthToken" not in rows
        and "jetskiStateSync.agentManagerInitState" not in rows
    ):
        raise RuntimeError("Saved GUI snapshot has no OAuth token")
    path = DB_PATHS[target]
    if not path.is_file():
        raise RuntimeError("%s state.vscdb not found" % target)
    save_vault_snapshot(profile_id, target, snapshot)

    bundle_id = BUNDLE_IDS[target]
    was_running = app_is_running(bundle_id)
    if was_running:
        stop_app(bundle_id)
    try:
        backup_database(path, target)
        connection = sqlite3.connect(path, timeout=5)
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.executemany("DELETE FROM ItemTable WHERE key = ?", ((key,) for key in AUTH_KEYS))
            connection.executemany(
                "INSERT INTO ItemTable(key, value) VALUES (?, ?)",
                rows.items(),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
    finally:
        if was_running:
            with contextlib.suppress(Exception):
                open_app(bundle_id)

    state = load_state()
    state["active"][target] = profile_id
    write_json_atomic(STATE_FILE, state)


def switch(profile_id, target):
    if target in ("all", "both"):
        state = load_state()
        profile = next((item for item in state["profiles"] if item.get("id") == profile_id), None)
        if not profile:
            raise RuntimeError("Antigravity profile not found: %s" % profile_id)
        targets = [t for t in ("ide", "cli") if t in profile.get("targets", [])]
        if not targets:
            targets = profile.get("targets", [])
        for t in targets:
            switch(profile_id, t)
        return {
            "ok": True,
            "profile_id": profile_id,
            "email": profile.get("email"),
            "target": "all",
        }
    snapshot = load_snapshot(profile_id, target)
    if target in DB_PATHS:
        switch_gui(profile_id, target, snapshot)
    elif target == "cli":
        if snapshot.get("kind") != "keychain" or not snapshot.get("payload"):
            raise RuntimeError("Saved CLI snapshot is invalid")
        was_running = app_is_running(SHARED_APP_BUNDLE_ID)
        if was_running:
            stop_app(SHARED_APP_BUNDLE_ID)
        try:
            run_keychain("set", CLI_SERVICE, CLI_ACCOUNT, snapshot["payload"].encode())
            write_cli_credentials_file(snapshot)
            state = load_state()
            state["active"][target] = profile_id
            write_json_atomic(STATE_FILE, state)
        finally:
            if was_running:
                open_app(SHARED_APP_BUNDLE_ID)
    else:
        raise RuntimeError("Unknown Antigravity target: %s" % target)
    profile = next((item for item in load_state()["profiles"] if item.get("id") == profile_id), None)
    return {
        "ok": True,
        "profile_id": profile_id,
        "email": profile.get("email") if profile else None,
        "target": target,
    }


def begin_login(target):
    if target not in (*DB_PATHS, "cli"):
        raise RuntimeError("Unknown Antigravity target: %s" % target)
    state = load_state()
    if state["pending"]:
        raise RuntimeError("Finish or cancel the current Antigravity login first")

    previous_profile_id = state["active"].get(target)
    previous_digest = None
    try:
        captured = capture(target)
        previous_profile_id = captured["profile"]["id"]
        previous_digest = snapshot_digest(read_current_snapshot(target))
    except Exception:
        pass

    pending = {
        "previous_profile_id": previous_profile_id,
        "previous_digest": previous_digest,
        "started_at": int(time.time()),
    }
    worker = None
    result_path = Path(os.environ.get(
        "KEYSWITCHER_ANTIGRAVITY_%s_LOGIN_RESULT" % target.upper(),
        ROOT / (target + "-login-result.json"),
    ))
    with contextlib.suppress(OSError):
        result_path.unlink()
    pending["oauth_result_path"] = str(result_path)
    if os.environ.get("KEYSWITCHER_ANTIGRAVITY_SKIP_APP_CONTROL") != "1":
        worker = compat.popen_detached(
            [
                sys.executable,
                str(Path(__file__).resolve()),
                "_oauth-worker",
                target,
                str(result_path),
            ],
        )
        pending["oauth_worker_pid"] = worker.pid

    state = load_state()
    state["pending"][target] = pending

    try:
        write_json_atomic(STATE_FILE, state)
    except Exception:
        if worker:
            with contextlib.suppress(OSError):
                os.kill(worker.pid, signal.SIGTERM)
        cancel_login(target)
        raise
    return {"ok": True, "pending": True, "target": target}


def finish_login(target):
    state = load_state()
    pending = state["pending"].get(target)
    if not pending:
        raise RuntimeError("No pending Antigravity login for %s" % target)
    result_path_value = pending.get("oauth_result_path")
    if result_path_value:
        result_path = Path(result_path_value)
        if not result_path.is_file():
            if pending.get("oauth_worker_pid") and not process_is_running(pending["oauth_worker_pid"]):
                state["pending"].pop(target, None)
                write_json_atomic(STATE_FILE, state)
                raise RuntimeError("Antigravity authorization stopped before completion")
            return {"ok": True, "pending": True, "target": target}
        oauth_result = json.loads(result_path.read_text())
        if not oauth_result.get("ok"):
            with contextlib.suppress(OSError):
                result_path.unlink()
            state["pending"].pop(target, None)
            write_json_atomic(STATE_FILE, state)
            raise RuntimeError(oauth_result.get("error") or "Antigravity authorization failed")
        credentials = oauth_result.get("credentials") or {}
        if target == "ide":
            snapshot = {
                "kind": "sqlite",
                "rows": {
                    "antigravityUnifiedStateSync.oauthToken": encode_ide_oauth_state(credentials),
                },
            }
            profile = save_snapshot(oauth_result["email"], target, snapshot, activate=False)
        elif target == "cli":
            snapshot = {
                "kind": "keychain",
                "payload": encode_cli_oauth_payload(oauth_result["email"], credentials),
            }
            profile = save_snapshot(oauth_result["email"], target, snapshot, activate=False)
        else:
            ide_snapshot = {
                "kind": "sqlite",
                "rows": {
                    "antigravityUnifiedStateSync.oauthToken": encode_ide_oauth_state(credentials),
                },
            }
            cli_snapshot = {
                "kind": "keychain",
                "payload": encode_cli_oauth_payload(oauth_result["email"], credentials),
            }
            save_snapshot(oauth_result["email"], "ide", ide_snapshot, activate=False)
            profile = save_snapshot(oauth_result["email"], "cli", cli_snapshot, activate=False)
        with contextlib.suppress(OSError):
            result_path.unlink()
        state = load_state()
        state["pending"].pop(target, None)
        write_json_atomic(STATE_FILE, state)
        return {"ok": True, "pending": False, "profile": profile, "target": target}
    try:
        current = read_current_snapshot(target)
        current_digest = snapshot_digest(current)
    except Exception:
        return {"ok": True, "pending": True, "target": target}
    if current_digest == pending.get("previous_digest"):
        return {"ok": True, "pending": True, "target": target}

    result = capture(target)
    state = load_state()
    state["pending"].pop(target, None)
    write_json_atomic(STATE_FILE, state)
    result["pending"] = False
    return result


def cancel_login(target):
    state = load_state()
    pending = state["pending"].get(target)
    if not pending:
        return {"ok": True, "pending": False, "target": target}
    worker_pid = pending.get("oauth_worker_pid")
    if worker_pid and process_is_running(worker_pid):
        with contextlib.suppress(OSError):
            os.kill(int(worker_pid), signal.SIGTERM)
    result_path = pending.get("oauth_result_path")
    if result_path:
        with contextlib.suppress(OSError):
            Path(result_path).unlink()
    previous_profile_id = pending.get("previous_profile_id")
    if previous_profile_id:
        switch(previous_profile_id, target)
    elif target in DB_PATHS:
        clear_gui_auth(target)
    else:
        run_keychain("delete", CLI_SERVICE, CLI_ACCOUNT, allow_missing=True)
    state = load_state()
    state["pending"].pop(target, None)
    if previous_profile_id:
        state["active"][target] = previous_profile_id
    else:
        state["active"].pop(target, None)
    write_json_atomic(STATE_FILE, state)
    return {"ok": True, "pending": False, "target": target}


def remove_profile_target(profile_id, target):
    if target in ("all", "both"):
        state = load_state()
        profile = next((item for item in state["profiles"] if item.get("id") == profile_id), None)
        if not profile:
            return {"ok": True, "profile_id": profile_id, "target": "all"}
        for t in list(profile.get("targets", [])):
            try:
                remove_profile_target(profile_id, t)
            except Exception:
                pass
        return {"ok": True, "profile_id": profile_id, "target": "all"}
    state = load_state()
    if target in state["pending"]:
        raise RuntimeError("Finish or cancel the current Antigravity login first")
    profile = next((item for item in state["profiles"] if item.get("id") == profile_id), None)
    if not profile or target not in profile.get("targets", []):
        raise RuntimeError("Antigravity profile is not saved for %s" % target)
    delete_vault_snapshot(profile_id, target)
    run_keychain(
        "delete", PROFILE_SERVICE, profile_account(profile_id, target), allow_missing=True,
    )
    profile["targets"] = [item for item in profile["targets"] if item != target]
    if state["active"].get(target) == profile_id:
        state["active"].pop(target, None)
    state["order"][target] = [item for item in state["order"][target] if item != profile_id]
    if not profile["targets"]:
        state["profiles"].remove(profile)
    write_json_atomic(STATE_FILE, state)
    return {"ok": True, "profile_id": profile_id, "target": target}


def sync_active_identities(state, include_cli=None):
    """Refresh active profile emails from live app/CLI state.

    IDE state is synced from local sqlite (state.vscdb) without Keychain prompts.
    CLI reads hit the shared login-keychain item service=gemini, which can trigger
    interactive Keychain prompts if the CLI refreshed its OAuth token. Routine
    status polls leave include_cli=False and rely on KeySwitcher-owned snapshots;
    explicit user actions (manual refresh, capture, switch) pass include_cli=True.
    """
    if include_cli is None:
        include_cli = os.environ.get("KEYSWITCHER_ANTIGRAVITY_SYNC_CLI") == "1"
    now = int(time.time())
    changed = False
    for target, old_profile_id in list(state["active"].items()):
        if target == "cli" and not include_cli:
            continue
        try:
            snapshot = read_current_snapshot(target)
            email = google_email(snapshot_credentials(snapshot))
        except Exception:
            continue

        new_profile_id = profile_id_for_email(email)
        old_profile = next(
            (item for item in state["profiles"] if item.get("id") == old_profile_id), None,
        )
        if new_profile_id == old_profile_id:
            if old_profile:
                old_profile["email"] = email
        else:
            old_snapshot_is_mislabeled = False
            try:
                saved_snapshot = load_snapshot(old_profile_id, target)
                saved_email = google_email(snapshot_credentials(saved_snapshot))
                old_snapshot_is_mislabeled = profile_id_for_email(saved_email) == new_profile_id
            except Exception:
                pass
            profile = next(
                (item for item in state["profiles"] if item.get("id") == new_profile_id), None,
            )
            if profile is None:
                profile = {"id": new_profile_id, "email": email, "targets": []}
                if old_snapshot_is_mislabeled and old_profile and old_profile.get("quota"):
                    profile["quota"] = old_profile["quota"]
                state["profiles"].append(profile)
            profile["email"] = email
            profile["targets"] = sorted(set(profile.get("targets", [])) | {target})
            profile["updated_at"] = now
            save_vault_snapshot(new_profile_id, target, snapshot)
            if old_snapshot_is_mislabeled:
                delete_vault_snapshot(old_profile_id, target)
                run_keychain(
                    "delete", PROFILE_SERVICE, profile_account(old_profile_id, target),
                    allow_missing=True,
                )
            if old_snapshot_is_mislabeled and old_profile:
                old_profile["targets"] = [
                    item for item in old_profile.get("targets", []) if item != target
                ]
                if not old_profile["targets"]:
                    state["profiles"].remove(old_profile)
            if old_snapshot_is_mislabeled:
                state["order"][target] = list(dict.fromkeys(
                    new_profile_id if item == old_profile_id else item
                    for item in state["order"][target]
                ))
            elif new_profile_id not in state["order"][target]:
                state["order"][target].append(new_profile_id)
            state["active"][target] = new_profile_id
        changed = True
    if changed:
        state["profiles"].sort(key=lambda item: item.get("email", "").lower())
        write_json_atomic(STATE_FILE, state)


def profile_quota_source(profile, state):
    profile_id = profile.get("id")
    # Prefer KeySwitcher-owned snapshots (service=com.eugene.keyswitcher.antigravity).
    # Live CLI state reads service=gemini and triggers Keychain prompts on poll,
    # so we never read live CLI state as a fallback during background quota checks.
    for target in profile.get("targets", []):
        try:
            return snapshot_credentials(load_snapshot(profile_id, target))
        except Exception:
            continue
    for target, active_profile_id in state["active"].items():
        if active_profile_id != profile_id or target == "cli":
            continue
        try:
            snapshot = read_current_snapshot(target)
            return snapshot_credentials(snapshot)
        except Exception:
            continue
    raise RuntimeError("No readable credentials for Antigravity profile")


def refresh_quotas(state):
    if os.environ.get("KEYSWITCHER_ANTIGRAVITY_SKIP_QUOTA") == "1":
        return
    now = int(time.time())
    jobs = []
    for index, profile in enumerate(state["profiles"]):
        quota = profile.get("quota") or {}
        if now - int(quota.get("updated_at", 0)) < 60:
            continue
        try:
            credentials = profile_quota_source(profile, state)
            jobs.append((index, credentials["access_token"], credentials.get("refresh_token")))
        except Exception:
            mark_quota_stale(profile)

    def fetch(job):
        index, access_token, refresh_token = job
        try:
            return index, antigravity_quota.fetch_quota(access_token, refresh_token)
        except Exception:
            return index, (None, None)

    if jobs:
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(4, len(jobs))) as executor:
            for index, result in executor.map(fetch, jobs):
                profile = state["profiles"][index]
                if isinstance(result, tuple) and len(result) == 2:
                    quota_groups, plan = result
                else:
                    quota_groups, plan = result, None
                if plan:
                    profile["plan"] = plan
                if quota_groups is None:
                    mark_quota_stale(profile)
                    continue
                for group in quota_groups.values():
                    group["fetched_at"] = now
                    group["stale"] = False
                profile["quota"] = {**quota_groups, "updated_at": now}
    write_json_atomic(STATE_FILE, state)


def mark_quota_stale(profile):
    quota = profile.get("quota")
    if not isinstance(quota, dict):
        profile["quota"] = {
            "gemini": {"ok": False, "stale": True, "error": "quota_unavailable"},
            "third_party": {"ok": False, "stale": True, "error": "quota_unavailable"},
            "updated_at": 0,
        }
        return
    for key in ("gemini", "third_party"):
        group = quota.get(key)
        if isinstance(group, dict):
            group["stale"] = True


def quota_window_used(group, key):
    try:
        return float((group.get(key) or {}).get("used_percent") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def exhausted_quota_groups(profile):
    quota = profile.get("quota") or {}
    exhausted = []
    for key in ("gemini", "third_party"):
        group = quota.get(key)
        if not isinstance(group, dict) or group.get("ok") is not True or group.get("stale") is True:
            continue
        if (
            group.get("allowed") is False
            or quota_window_used(group, "primary") >= LIMIT_EXHAUSTED_PERCENT
            or quota_window_used(group, "secondary") >= LIMIT_EXHAUSTED_PERCENT
        ):
            exhausted.append(key)
    return exhausted


def candidate_quota_score(profile, exhausted_groups):
    quota = profile.get("quota") or {}
    used = []
    for key in exhausted_groups:
        group = quota.get(key)
        if (
            not isinstance(group, dict)
            or group.get("ok") is not True
            or group.get("stale") is True
            or group.get("allowed") is False
        ):
            return None
        group_used = [
            quota_window_used(group, "primary"),
            quota_window_used(group, "secondary"),
        ]
        if max(group_used) >= LIMIT_EXHAUSTED_PERCENT:
            return None
        used.extend(group_used)
    return max(used), sum(used)


def autoswitch_candidate(state, target):
    active_id = state["active"].get(target)
    active = next(
        (profile for profile in state["profiles"] if profile.get("id") == active_id),
        None,
    )
    if active is None:
        return None, [], "no_active_profile"
    exhausted_groups = exhausted_quota_groups(active)
    if not exhausted_groups:
        return None, [], "below_limit"

    profiles = {profile.get("id"): profile for profile in state["profiles"]}
    order = state["order"].get(target, [])
    if active_id in order:
        active_index = order.index(active_id)
        order = order[active_index + 1:] + order[:active_index]
    for profile_id in order:
        profile = profiles.get(profile_id)
        if profile is None:
            continue
        if profile.get("id") == active_id or target not in profile.get("targets", []):
            continue
        if candidate_quota_score(profile, exhausted_groups) is not None:
            return profile, exhausted_groups, "limit_exhausted"
    return None, exhausted_groups, "no_candidate"


def reorder_profiles(target, profile_ids):
    if target in ("all", "both"):
        state = load_state()
        for t in TARGETS:
            available = set(
                profile["id"] for profile in state["profiles"]
                if t in profile.get("targets", [])
            )
            filtered = [pid for pid in profile_ids if pid in available]
            for pid in available:
                if pid not in filtered:
                    filtered.append(pid)
            state["order"][t] = filtered
        write_json_atomic(STATE_FILE, state)
        return {"ok": True, "order": state["order"], "target": target}

    if target not in TARGETS:
        raise RuntimeError("Unknown Antigravity target: %s" % target)
    state = load_state()
    available = [
        profile["id"] for profile in state["profiles"]
        if target in profile.get("targets", [])
    ]
    if len(profile_ids) != len(set(profile_ids)) or set(profile_ids) != set(available):
        raise RuntimeError("Order must contain every %s profile exactly once" % target)
    state["order"][target] = profile_ids
    write_json_atomic(STATE_FILE, state)
    return {"ok": True, "order": state["order"], "target": target}


def set_autoswitch(target, enabled):
    if target not in TARGETS:
        raise RuntimeError("Unknown Antigravity target: %s" % target)
    state = load_state()
    state["autoswitch"][target] = enabled
    write_json_atomic(STATE_FILE, state)
    return {"ok": True, "autoswitch": state["autoswitch"], "target": target}


def auto_check():
    state = load_state()
    result = {
        "ok": True,
        "switched_targets": [],
        "switches": [],
        "reasons": {},
        "errors": {},
    }
    if not any(state["autoswitch"].values()):
        result["reasons"] = {target: "disabled" for target in TARGETS}
        return result
    if state["pending"]:
        result["reasons"] = {target: "login_pending" for target in TARGETS}
        return result

    sync_active_identities(state)
    state = load_state()
    refresh_quotas(state)
    for target in sorted(TARGETS):
        if state["autoswitch"].get(target) is not True:
            result["reasons"][target] = "disabled"
            continue
        candidate, groups, reason = autoswitch_candidate(state, target)
        result["reasons"][target] = reason
        if candidate is None:
            continue
        try:
            switched = switch(candidate["id"], target)
        except Exception as exc:
            result["ok"] = False
            result["errors"][target] = str(exc)
            continue
        result["switched_targets"].append(target)
        result["switches"].append({
            **switched,
            "quota_groups": groups,
        })
    return result


def status(sync_cli=False):
    state = load_state()
    sync_active_identities(state, include_cli=sync_cli)
    refresh_quotas(state)
    targets = {
        "ide": {"available": DB_PATHS["ide"].is_file(), "installed": APP_PATHS["ide"].is_dir()},
        "cli": {
            "available": keychain_helper_available(),
            "installed": SHARED_APP_PATH.is_dir() and find_cli_binary() is not None,
        },
    }
    return {
        "ok": True,
        "profiles": state["profiles"],
        "order": state["order"],
        "active": state["active"],
        "autoswitch": state["autoswitch"],
        "targets": targets,
        "pending_targets": sorted(state["pending"]),
    }


def keychain_helper_available():
    if os.environ.get("KEYSWITCHER_ANTIGRAVITY_KEYCHAIN_HELPER"):
        return Path(os.environ["KEYSWITCHER_ANTIGRAVITY_KEYCHAIN_HELPER"]).is_file()
    try:
        keychain_helper_path()
        return True
    except RuntimeError:
        return compat.IS_WIN


def main(args):
    try:
        with exclusive_lock():
            if not args or args[0] == "status":
                sync_cli = "--sync-cli" in args or os.environ.get("KEYSWITCHER_ANTIGRAVITY_SYNC_CLI") == "1"
                return status(sync_cli=sync_cli)
            if args[0] == "sync":
                target = args[1] if len(args) > 1 else "all"
                state = load_state()
                include_cli = target in ("all", "cli")
                sync_active_identities(state, include_cli=include_cli)
                return status(sync_cli=False)
            if args[0] == "capture" and len(args) == 2:
                return capture(args[1])
            if args[0] == "switch" and len(args) == 3:
                return switch(args[1], args[2])
            if args[0] == "begin-login" and len(args) == 2:
                return begin_login(args[1])
            if args[0] == "finish-login" and len(args) == 2:
                return finish_login(args[1])
            if args[0] == "cancel-login" and len(args) == 2:
                return cancel_login(args[1])
            if args[0] == "remove" and len(args) == 3:
                return remove_profile_target(args[1], args[2])
            if args[0] == "reorder" and len(args) >= 3:
                return reorder_profiles(args[1], args[2:])
            if args[0] == "set-auto" and len(args) == 3 and args[2] in ("on", "off"):
                return set_autoswitch(args[1], args[2] == "on")
            if args[0] == "auto-check" and len(args) == 1:
                return auto_check()
            return {"ok": False, "error": "usage: antigravity <status [--sync-cli]|sync [TARGET]|capture TARGET|switch PROFILE TARGET|begin-login TARGET|finish-login TARGET|cancel-login TARGET|remove PROFILE TARGET|reorder TARGET PROFILE...|set-auto TARGET on|off|auto-check>"}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "_oauth-worker":
        run_oauth_worker(sys.argv[2], sys.argv[3])
    else:
        print(json.dumps(main(sys.argv[1:])))
