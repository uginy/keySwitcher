#!/usr/bin/env python3
"""KeySwitcher engine: JSON CLI backend for the KeySwitcher menu bar / tray app.

This is the single source of truth the Swift menu bar app shells out to. It
manages the saved Codex account slots in ~/.codex/accounts/auth_N.json,
reports per-account rate-limit usage from the ChatGPT wham/usage API,
refreshes expired OAuth access tokens for *inactive* slots, and switches
the active account through the bundled rotator.py.

Contract: every invocation prints exactly ONE JSON object to stdout (ASCII
escaped, single line). Exit code is 0 even for handled errors (reported in
the JSON); nonzero only on unexpected/catastrophic failure.

Security: OAuth token values are never printed, logged, or included in any
JSON output. Files that persist tokens are written atomically (tempfile in
the same directory + os.replace) with mode 0600. Token refreshes and
engine-initiated switches are serialized with an exclusive fcntl lock on
~/.codex/keyswitcher/.lock. The ACTIVE slot's tokens are never refreshed
here: the Codex desktop app owns auth.json refreshes and the bundled daemon
syncs them back to slots.
"""

import base64
import concurrent.futures
import contextlib
import json
import os
import shutil
import signal
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.dont_write_bytecode = True

import antigravity as antigravity_engine
import compat

HOME = Path.home()
CODEX_DIR = HOME / ".codex"
ACCOUNTS_DIR = CODEX_DIR / "accounts"
AUTH_FILE = CODEX_DIR / "auth.json"
KS_DIR = CODEX_DIR / "keyswitcher"
CONFIG_FILE = KS_DIR / "config.json"
CACHE_FILE = KS_DIR / "cache.json"
STATE_FILE = KS_DIR / "state.json"
LOCK_FILE = KS_DIR / ".lock"

USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
TOKEN_URL = "https://auth.openai.com/oauth/token"
USER_AGENT = "Codex/0.138.0"
HTTP_TIMEOUT = 10  # seconds, per request
TOKEN_EXP_MARGIN = 120  # refresh tokens expiring within this many seconds
REFRESH_FAIL_COOLDOWN = 3600  # per-slot backoff after a failed refresh
LIMIT_EXHAUSTED_PERCENT = 99.0  # 1% remaining or less is close enough to blocked

LAUNCH_AGENT_LABEL = "com.codex.rotator"
LAUNCH_AGENT_PLIST = compat.launch_agent_plist()
APP_SUPPORT_DIR = compat.keyswitcher_support_dir()
LOG_DIR = compat.keyswitcher_log_dir()
ROTATOR_NAME = "rotator.py"
LAUNCH_ROTATOR = APP_SUPPORT_DIR / ROTATOR_NAME
DAEMON_PATTERN = str(LAUNCH_ROTATOR)
KS_DAEMON_PATTERN = str(KS_DIR / ROTATOR_NAME)
LEGACY_DAEMON_PATTERN = str(CODEX_DIR / "daemon.py")

RELOGIN_TIMEOUT = 300  # seconds to allow for the interactive browser login

DEFAULT_CONFIG = {
    "autoswitch_enabled": False,  # opt-in: ON drives the silent reactive daemon
    "notifications": True,
    "tray_display": "both",
    "antigravity_tray_target": "both",
    "antigravity_tray_models": "both",
    "tray_slots": ["codex", "ag_cli_gemini", "ag_cli_claude", "ag_ide_gemini", "ag_ide_claude"],
}
DEFAULT_STATE = {"cooldown_until": 0, "refresh_failures": {}}

# Globs used to locate the native codex binary when the GUI app has a minimal
# PATH or when `codex` resolves to the Node wrapper script.
CODEX_NATIVE_GLOBS = [
    "node_modules/@openai/codex/node_modules/@openai/codex-*/vendor/*/codex/codex",
    "node_modules/@openai/codex/node_modules/@openai/codex-*/vendor/*/bin/codex",
    "node_modules/@openai/codex/node_modules/@openai/codex-*/vendor/*/bin/codex.exe",
    "node_modules/@openai/codex-*/vendor/*/bin/codex",
    "node_modules/@openai/codex-*/vendor/*/bin/codex.exe",
    "@openai/codex-*/vendor/*/bin/codex",
    "@openai/codex-*/vendor/*/bin/codex.exe",
    "@openai/codex/node_modules/@openai/codex-*/vendor/*/bin/codex.exe",
]
CODEX_GLOB_ROOTS = [
    HOME / ".bun/install/global",
    HOME / ".nvm/versions/node",  # searched as <root>/*/lib/<glob>
    Path("/usr/local/lib"),
    Path("/opt/homebrew/lib"),
] + compat.extra_codex_glob_roots()

_SSL_CONTEXT = None


# --------------------------------------------------------------------------
# Small utilities
# --------------------------------------------------------------------------

def ssl_context():
    global _SSL_CONTEXT
    if _SSL_CONTEXT is None:
        _SSL_CONTEXT = ssl.create_default_context()
    return _SSL_CONTEXT


def iso_utc_now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def read_json(path, default=None):
    try:
        with open(path, "r") as fh:
            return json.load(fh)
    except Exception:
        return default


def write_json_atomic(path, data, mode=0o600):
    """Atomically write JSON: tempfile in the same dir, chmod, os.replace."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix="." + path.name + ".")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(data, fh, indent=2)
        compat.secure_chmod(tmp, mode)
        os.replace(tmp, path)
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


@contextlib.contextmanager
def exclusive_lock():
    """Exclusive advisory lock shared by every keyswitcher process."""
    with compat.exclusive_lock(LOCK_FILE):
        yield


# --------------------------------------------------------------------------
# JWT helpers (urlsafe base64 with correct padding)
# --------------------------------------------------------------------------

def jwt_payload(token):
    try:
        part = token.split(".")[1]
        part += "=" * (-len(part) % 4)
        return json.loads(base64.urlsafe_b64decode(part.encode("ascii")))
    except Exception:
        return {}


def email_from_payload(payload):
    email = payload.get("email")
    if not email:
        email = (payload.get("https://api.openai.com/profile") or {}).get("email")
    return email


def email_from_tokens(tokens):
    for key in ("id_token", "access_token"):
        token = tokens.get(key)
        if token:
            email = email_from_payload(jwt_payload(token))
            if email:
                return email
    return None


def token_expiring(token, margin=TOKEN_EXP_MARGIN):
    exp = jwt_payload(token).get("exp")
    return bool(exp) and exp < time.time() + margin


# --------------------------------------------------------------------------
# Config / state / cache
# --------------------------------------------------------------------------

def load_config():
    cfg = dict(DEFAULT_CONFIG)
    stored = read_json(CONFIG_FILE, {})
    if isinstance(stored, dict):
        cfg.update(stored)
    cfg.pop("threshold", None)
    cfg.pop("cache_ttl", None)
    return cfg


def save_config(cfg):
    write_json_atomic(CONFIG_FILE, cfg, 0o600)


def load_state():
    state = dict(DEFAULT_STATE)
    stored = read_json(STATE_FILE, {})
    if isinstance(stored, dict):
        state.update(stored)
    if not isinstance(state.get("refresh_failures"), dict):
        state["refresh_failures"] = {}
    return state


def save_state(state):
    write_json_atomic(STATE_FILE, state, 0o600)


def load_cache():
    cache = read_json(CACHE_FILE, {})
    return cache if isinstance(cache, dict) else {}


def merge_cache_updates(updates):
    """Re-read, merge and persist cache entries under the global lock."""
    if not updates:
        return
    with exclusive_lock():
        cache = load_cache()
        cache.update(updates)
        write_json_atomic(CACHE_FILE, cache, 0o600)


def record_refresh_failures(state, account_ids, now):
    if not account_ids:
        return
    with exclusive_lock():
        fresh = load_state()
        for account_id in account_ids:
            fresh["refresh_failures"][account_id] = now
        state["refresh_failures"].update(fresh["refresh_failures"])
        save_state(fresh)


# --------------------------------------------------------------------------
# OAuth client_id extraction (one-time, cached in config.json)
# --------------------------------------------------------------------------

def _scan_app_ids(blob):
    """Find app_<alnum> runs in raw binary data."""
    found = set()
    pos = blob.find(b"app_")
    while pos != -1:
        end = pos + 4
        while end < len(blob):
            ch = blob[end]
            if 48 <= ch <= 57 or 65 <= ch <= 90 or 97 <= ch <= 122:
                end += 1
            else:
                break
        if end - pos >= 14:  # app_ + at least 10 chars
            found.add(blob[pos:end].decode("ascii", "replace"))
        pos = blob.find(b"app_", end)
    return found


def _common_prefix(strings):
    low, high = min(strings), max(strings)
    i = 0
    while i < min(len(low), len(high)) and low[i] == high[i]:
        i += 1
    return low[:i]


def _pick_client_id(candidates):
    """Recover the real OAuth client_id from a binary string scan.

    In the Rust string table the genuine `app_<random>` client_id is
    concatenated with several different following symbols, so it shows up as
    the common prefix of a group of candidates sharing a stem. Dictionary-word
    decoys (app_metadatalabels, app_serverinput, ...) also group, so we
    disambiguate by entropy: the genuine id has an alphanumeric body mixing
    upper case, lower case and digits.
    """
    if not candidates:
        return None
    groups = {}
    for cand in candidates:
        groups.setdefault(cand[:15], []).append(cand)
    prefixes = []
    for members in groups.values():
        prefix = _common_prefix(members) if len(members) >= 2 else members[0]
        prefixes.append((prefix, len(members)))

    def score(item):
        prefix, count = item
        body = prefix[4:]
        alnum = body.isalnum()
        entropy = (any(c.isupper() for c in body)
                   and any(c.islower() for c in body)
                   and any(c.isdigit() for c in body))
        return (alnum, entropy, count >= 2, len(prefix))

    prefixes.sort(key=score, reverse=True)
    return prefixes[0][0]


def _unique_existing_paths(candidates):
    return compat.unique_existing_runnables(candidates)


def _codex_binary_candidates():
    candidates = []
    try:
        found = shutil.which("codex") or ""
        if not found and not compat.IS_WIN:
            result = subprocess.run(
                ["/bin/sh", "-c", "command -v codex"],
                capture_output=True, text=True, timeout=10,
            )
            found = result.stdout.strip()
        if found:
            real = Path(found).resolve()
            if real.name.lower() not in ("codex.js",):
                candidates.append(real)
            # Node wrapper lives at <pkg>/bin/codex.js; native binary is in
            # the platform sub-package under the same package root.
            search_roots = [real.parent, real.parent.parent]
            if compat.IS_WIN:
                npm_modules = Path(os.environ.get("APPDATA", "")) / "npm" / "node_modules"
                search_roots.append(npm_modules)
            for pkg_root in search_roots:
                candidates.extend(sorted(pkg_root.glob("node_modules/@openai/codex-*/vendor/*/codex/codex")))
                candidates.extend(sorted(pkg_root.glob("node_modules/@openai/codex-*/vendor/*/bin/codex")))
                candidates.extend(sorted(pkg_root.glob("node_modules/@openai/codex-*/vendor/*/bin/codex.exe")))
                candidates.extend(sorted(pkg_root.glob("@openai/codex-*/vendor/*/bin/codex.exe")))
                candidates.extend(sorted(pkg_root.glob("@openai/codex/node_modules/@openai/codex-*/vendor/*/bin/codex.exe")))
                candidates.extend(sorted(pkg_root.parent.glob("codex-*/vendor/*/bin/codex")))
                candidates.extend(sorted(pkg_root.parent.glob("codex-*/vendor/*/bin/codex.exe")))
    except Exception:
        pass
    for root in CODEX_GLOB_ROOTS:
        if not root.exists():
            continue
        if root.name == "node":  # nvm: <root>/<version>/lib/<glob>
            for glob in CODEX_NATIVE_GLOBS:
                candidates.extend(sorted(root.glob("*/lib/" + glob)))
        else:
            for glob in CODEX_NATIVE_GLOBS:
                candidates.extend(sorted(root.glob(glob)))
    return _unique_existing_paths(candidates)


def resolve_codex_cli():
    candidates = []
    env_path = os.environ.get("KEYSWITCHER_CODEX_CLI")
    if env_path:
        candidates.append(Path(env_path).expanduser())
    candidates.extend(_codex_binary_candidates())
    found = shutil.which("codex")
    if found:
        candidates.append(Path(found))
    candidates.extend(compat.extra_cli_candidates())
    candidates.extend([
        HOME / ".local/bin/codex",
        HOME / ".bun/bin/codex",
        Path("/opt/homebrew/bin/codex"),
        Path("/usr/local/bin/codex"),
    ])
    existing = _unique_existing_paths(candidates)
    return str(existing[0]) if existing else None


def codex_login_env(tmp_home):
    env = dict(os.environ)
    env["CODEX_HOME"] = str(tmp_home)
    extra_path = [str(path) for path in compat.extra_codex_path_dirs()]
    env["PATH"] = os.pathsep.join(extra_path + [env.get("PATH", "")])
    return env


def _kill_login_process(pid):
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    try:
        os.killpg(os.getpgid(pid), signal.SIGTERM)
        return True
    except OSError:
        pass
    try:
        os.kill(pid, signal.SIGTERM)
        return True
    except OSError:
        return False


def run_codex_login(tmp_home, pidfile=None):
    """Run interactive `codex login`; record child pid so it can be cancelled."""
    codex_cli = resolve_codex_cli()
    if not codex_cli:
        raise FileNotFoundError("codex CLI not found")
    proc = subprocess.Popen(
        [codex_cli, "login"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        env=codex_login_env(tmp_home),
        start_new_session=True,
    )
    if pidfile is not None:
        write_json_atomic(pidfile, {"pid": proc.pid}, 0o600)
    try:
        stdout, _stderr = proc.communicate(timeout=RELOGIN_TIMEOUT)
    except subprocess.TimeoutExpired:
        _kill_login_process(proc.pid)
        proc.communicate()
        raise
    finally:
        if pidfile is not None:
            with contextlib.suppress(OSError):
                pidfile.unlink()
    return proc.returncode, stdout


def kill_pending_logins():
    """Terminate codex login processes started by add/relogin (cancel-add)."""
    names = ["add.pid"]
    names += ["relogin_%d.pid" % s["slot"] for s in discover_slots()]
    stopped = False
    for name in names:
        path = KS_DIR / name
        data = read_json(path) or {}
        if path.exists():
            with contextlib.suppress(OSError):
                path.unlink()
        if data.get("pid") and _kill_login_process(data["pid"]):
            stopped = True
    return stopped


def extract_client_id():
    for binary in _codex_binary_candidates():
        try:
            with open(binary, "rb") as fh:
                ids = _scan_app_ids(fh.read())
        except Exception:
            continue
        client_id = _pick_client_id(ids)
        if client_id:
            return client_id
    return None


def get_client_id(cfg):
    client_id = cfg.get("client_id")
    if client_id:
        return client_id
    client_id = extract_client_id()
    if client_id:
        cfg["client_id"] = client_id
        save_config(cfg)
    return client_id


# --------------------------------------------------------------------------
# Token refresh (inactive slots only)
# --------------------------------------------------------------------------

def refresh_slot_tokens(slot_path, cfg):
    """Refresh an inactive slot's access token; persist atomically (0600).

    Runs under the exclusive lock so concurrent invocations never refresh
    the same slot twice (the second one re-reads and sees a fresh token).
    Returns the updated auth data dict. Raises on failure.
    """
    with exclusive_lock():
        data = read_json(slot_path)
        if not isinstance(data, dict):
            raise RuntimeError("slot file unreadable")
        tokens = data.get("tokens") or {}
        if not token_expiring(tokens.get("access_token", "")):
            return data  # already refreshed by another process
        client_id = get_client_id(cfg)
        if not client_id:
            raise RuntimeError("oauth client_id unavailable")
        refresh_token = tokens.get("refresh_token")
        if not refresh_token:
            raise RuntimeError("slot has no refresh_token")
        body = json.dumps({
            "client_id": client_id,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "scope": "openid profile email",
        }).encode("utf-8")
        request = urllib.request.Request(
            TOKEN_URL, data=body, method="POST",
            headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
        )
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT, context=ssl_context()) as resp:
            fresh = json.loads(resp.read().decode("utf-8"))
        if not fresh.get("access_token"):
            raise RuntimeError("refresh response missing access_token")
        tokens["access_token"] = fresh["access_token"]
        for key in ("id_token", "refresh_token"):
            if fresh.get(key):
                tokens[key] = fresh[key]
        data["tokens"] = tokens
        data["last_refresh"] = iso_utc_now()
        write_json_atomic(slot_path, data, 0o600)
        return data


# --------------------------------------------------------------------------
# Usage API
# --------------------------------------------------------------------------

def fetch_usage_raw(access_token, account_id):
    request = urllib.request.Request(USAGE_URL, headers={
        "Authorization": "Bearer " + access_token,
        "chatgpt-account-id": account_id,
        "User-Agent": USER_AGENT,
    })
    with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT, context=ssl_context()) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _normalize_window(window):
    if not window:
        return None
    reset_at = window.get("reset_at")
    if reset_at is None:
        reset_at = time.time() + (window.get("reset_after_seconds") or 0)
    return {
        "used_percent": float(window.get("used_percent") or 0.0),
        "reset_at": int(reset_at),
        "window_minutes": int(window.get("limit_window_seconds") or 0) // 60,
    }


def normalize_usage(raw, fetched_at):
    rate_limit = raw.get("rate_limit") or {}
    primary_raw = rate_limit.get("primary_window")
    secondary_raw = rate_limit.get("secondary_window")

    # The usage API does not keep the old semantic names consistently. For
    # some plans (currently Pro Lite and Plus) the only returned window is a
    # 7-day window in `primary_window`, while K12 still returns a 5-hour
    # `primary_window` plus a weekly `secondary_window`. Normalize by duration
    # so the UI contract remains stable: primary = short window, secondary =
    # weekly window.
    primary = None
    secondary = None
    for window in (primary_raw, secondary_raw):
        if not window:
            continue
        try:
            duration = int(window.get("limit_window_seconds") or 0)
        except (TypeError, ValueError):
            duration = 0
        normalized = _normalize_window(window)
        if duration and duration <= 24 * 60 * 60 and primary is None:
            primary = normalized
        elif duration and duration > 24 * 60 * 60 and secondary is None:
            secondary = normalized

    # Backward compatibility for responses that omit the duration.
    if primary is None and secondary is None and primary_raw:
        primary = _normalize_window(primary_raw)
    if secondary is None and secondary_raw:
        secondary = _normalize_window(secondary_raw)

    try:
        balance = float((raw.get("credits") or {}).get("balance"))
    except (TypeError, ValueError):
        balance = None
    return {
        "ok": True,
        "fetched_at": fetched_at,
        "stale": False,
        "allowed": rate_limit.get("allowed"),
        "primary": primary,
        "secondary": secondary,
        "credits_balance": balance,
    }


def needs_usage_confirmation(usage, cached):
    cached_usage = (cached or {}).get("usage") or {}
    for name in ("primary", "secondary"):
        fresh = usage.get(name) or {}
        previous = cached_usage.get(name) or {}
        try:
            fresh_used = float(fresh.get("used_percent"))
            previous_used = float(previous.get("used_percent"))
        except (TypeError, ValueError):
            continue
        if ((fresh_used < 0.5 and previous_used >= 1.0)
                or previous_used - fresh_used >= 5.0):
            return True
    return False


def failed_usage(fetched_at):
    return {
        "ok": False,
        "fetched_at": fetched_at,
        "stale": False,
        "allowed": None,
        "primary": None,
        "secondary": None,
        "credits_balance": None,
    }


# --------------------------------------------------------------------------
# Slot discovery and per-account collection
# --------------------------------------------------------------------------

def discover_slots():
    slots = []
    if not ACCOUNTS_DIR.is_dir():
        return slots
    for path in sorted(ACCOUNTS_DIR.glob("auth_*.json")):
        try:
            number = int(path.stem.split("_", 1)[1])
        except (IndexError, ValueError):
            continue
        data = read_json(path)
        tokens = (data or {}).get("tokens") or {}
        slots.append({
            "slot": number,
            "path": path,
            "file": path.name,
            "readable": isinstance(data, dict),
            "tokens": tokens,
            "account_id": tokens.get("account_id"),
        })
    slots.sort(key=lambda s: s["slot"])
    return slots


def read_active_tokens():
    data = read_json(AUTH_FILE)
    return (data or {}).get("tokens") or {}


def _serve_stale(entry, cached):
    if cached and cached.get("usage"):
        usage = dict(cached["usage"])
        usage["stale"] = True
        entry["usage"] = usage
    else:
        entry["usage"] = failed_usage(int(time.time()))
    return entry


def _gather_one(slot, active_id, active_tokens, cfg, state, cache, now):
    """Build one contract account entry. Returns (entry, cache_update, failed_account_id)."""
    account_id = slot["account_id"]
    entry = {
        "slot": slot["slot"],
        "file": slot["file"],
        "email": email_from_tokens(slot["tokens"]),
        "account_id": account_id,
        "active": bool(account_id) and account_id == active_id,
        "plan": None,
        "usage": None,
        "error": None,
    }
    if not slot["readable"] or not account_id:
        entry["error"] = "slot file unreadable or missing account_id"
        return entry, None, None

    cached = cache.get(account_id)
    if cached:
        entry["email"] = cached.get("email") or entry["email"]
        entry["plan"] = cached.get("plan")

    tokens = active_tokens if (entry["active"] and active_tokens) else slot["tokens"]
    access_token = tokens.get("access_token") or ""

    if token_expiring(access_token) and not entry["active"]:
        failed_at = int(state.get("refresh_failures", {}).get(account_id, 0))
        if now - failed_at < REFRESH_FAIL_COOLDOWN:
            entry["usage"] = failed_usage(now)
            entry["error"] = "auth_expired"
            return entry, None, None
        try:
            data = refresh_slot_tokens(slot["path"], cfg)
            tokens = data.get("tokens") or {}
            access_token = tokens.get("access_token") or ""
            entry["email"] = email_from_tokens(tokens) or entry["email"]
        except Exception:
            entry["usage"] = failed_usage(now)
            entry["error"] = "auth_expired"
            return entry, None, account_id

    try:
        raw = fetch_usage_raw(access_token, account_id)
    except urllib.error.HTTPError as exc:
        entry["error"] = "auth_expired" if exc.code in (401, 403) else "http_%d" % exc.code
        return _serve_stale(entry, cached), None, None
    except Exception as exc:
        entry["error"] = "%s: %s" % (type(exc).__name__, exc)
        return _serve_stale(entry, cached), None, None

    usage = normalize_usage(raw, now)
    if needs_usage_confirmation(usage, cached):
        try:
            raw = fetch_usage_raw(access_token, account_id)
            usage = normalize_usage(raw, now)
        except urllib.error.HTTPError as exc:
            entry["error"] = "auth_expired" if exc.code in (401, 403) else "http_%d" % exc.code
            return _serve_stale(entry, cached), None, None
        except Exception as exc:
            entry["error"] = "%s: %s" % (type(exc).__name__, exc)
            return _serve_stale(entry, cached), None, None
    entry["usage"] = usage
    entry["email"] = raw.get("email") or entry["email"]
    entry["plan"] = raw.get("plan_type") or entry["plan"]
    update = {
        "fetched_at": now,
        "usage": usage,
        "email": entry["email"],
        "plan": entry["plan"],
    }
    return entry, (account_id, update), None


def collect_accounts(cfg, state):
    """Gather contract entries for all slots (usage fetched in parallel).

    Persists cache updates and refresh-failure cooldowns as a side effect.
    Returns (accounts, active_slot, active_account_id).
    """
    slots = discover_slots()
    active_tokens = read_active_tokens()
    active_id = active_tokens.get("account_id")
    cache = load_cache()
    now = int(time.time())

    # Prune cache entries for accounts that no longer exist in any slot.
    valid_ids = {s["account_id"] for s in slots if s["account_id"]}
    if active_id:
        valid_ids.add(active_id)
    if valid_ids and any(key not in valid_ids for key in cache):
        with exclusive_lock():
            fresh = {k: v for k, v in load_cache().items() if k in valid_ids}
            write_json_atomic(CACHE_FILE, fresh, 0o600)

    results = []
    if slots:
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(slots), 8)) as pool:
            results = list(pool.map(
                lambda s: _gather_one(s, active_id, active_tokens, cfg, state,
                                      cache, now),
                slots))

    accounts, updates, failures = [], {}, []
    for entry, update, failed_account in results:
        accounts.append(entry)
        if update:
            updates[update[0]] = update[1]
        if failed_account:
            failures.append(failed_account)

    merge_cache_updates(updates)
    record_refresh_failures(state, failures, now)

    active_slot = next(
        (e["slot"] for e in accounts if e["active"]), None)
    return accounts, active_slot, active_id


# --------------------------------------------------------------------------
# Switching (bundled rotator.py)
# --------------------------------------------------------------------------

def rotator_script_path():
    """rotator.py always sits next to this file — inside the app bundle's
    Resources, or in engine/ in the dev tree — never in ~/.codex. From here it
    gets copied into ~/Library/Application Support/KeySwitcher for the daemon."""
    return Path(__file__).resolve().with_name(ROTATOR_NAME)


def import_rotator():
    engine_dir = str(rotator_script_path().parent)
    if engine_dir not in sys.path:
        sys.path.insert(0, engine_dir)
    import rotator
    return rotator


def run_rotate(slot_number, expected_account_id, restart_app=True):
    """Run rotate(slot) with stdout captured; verify auth.json afterwards.

    rotate() itself restores the Codex thread after the restart, so the manual,
    confirm-modal and reactive-daemon paths all return to the same conversation.
    """
    rotator = import_rotator()
    log = []
    with tempfile.TemporaryFile(mode="w+") as buf:
        with contextlib.redirect_stdout(buf):
            rotator.rotate(slot_number, restart_app=restart_app)
        buf.seek(0)
        log = [line.rstrip("\n") for line in buf if line.strip()]
    new_active = read_active_tokens().get("account_id")
    ok = new_active == expected_account_id
    return ok, log


def best_email(slot, cache):
    cached = cache.get(slot["account_id"]) if slot["account_id"] else None
    if cached and cached.get("email"):
        return cached["email"]
    return email_from_tokens(slot["tokens"])


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

def daemon_running():
    patterns = [DAEMON_PATTERN, KS_DAEMON_PATTERN, LEGACY_DAEMON_PATTERN]
    try:
        return any(compat.command_running(pattern) for pattern in patterns)
    except Exception:
        return False


def cmd_status(args):
    cfg = load_config()
    state = load_state()
    accounts, active_slot, active_id = collect_accounts(cfg, state)
    return {
        "ok": True,
        "generated_at": int(time.time()),
        "active_slot": active_slot,
        "active_account_id": active_id,
        "daemon": {"running": daemon_running()},
        "autoswitch": {
            "enabled": bool(cfg.get("autoswitch_enabled", False)),
            "cooldown_until": int(state.get("cooldown_until", 0)),
        },
        "accounts": accounts,
    }


def cmd_switch(args):
    base = {"ok": False, "slot": None, "email": None, "error": None, "log": []}
    restart_app = "--no-restart" not in args
    positional = [arg for arg in args if arg != "--no-restart"]
    try:
        slot_number = int(positional[0])
    except (IndexError, ValueError):
        base["error"] = "usage: switch <slot> [--no-restart]"
        return base
    base["slot"] = slot_number

    target = next((s for s in discover_slots() if s["slot"] == slot_number), None)
    if target is None or not target["account_id"]:
        base["error"] = "no such slot: %d" % slot_number
        return base
    base["email"] = best_email(target, load_cache())

    if read_active_tokens().get("account_id") == target["account_id"]:
        base.update({"ok": True, "log": ["already active"]})
        return base

    ok, log = run_rotate(slot_number, target["account_id"], restart_app=restart_app)
    base.update({"ok": ok, "log": log})
    if not ok:
        base["error"] = "switch verification failed: auth.json does not match target slot"
    return base


def cmd_autoswitch_check(_args):
    base = {"ok": True, "switched": False, "from_slot": None,
            "to_slot": None, "to_email": None, "reason": ""}
    cfg = load_config()
    state = load_state()

    # No autoswitch_enabled gate here: the Swift app only calls this in the OFF
    # mode (to drive the suggestion modal). In the ON mode the reactive daemon
    # does the switching, so the app stays quiet and never polls this.
    accounts, active_slot, _active_id = collect_accounts(cfg, state)
    if active_slot is None:
        base["reason"] = "no_active_slot"
        return base

    active_entry = next(e for e in accounts if e["slot"] == active_slot)
    usage = active_entry.get("usage")
    if not usage or not usage.get("ok"):
        base["reason"] = "usage_unavailable"
        return base

    def window_used(u, key):
        return float(((u.get(key) or {}).get("used_percent")) or 0.0)

    blocked = usage.get("allowed") is False
    primary_exhausted = window_used(usage, "primary") >= LIMIT_EXHAUSTED_PERCENT
    secondary_exhausted = window_used(usage, "secondary") >= LIMIT_EXHAUSTED_PERCENT
    if not (blocked or primary_exhausted or secondary_exhausted):
        base["reason"] = "below_limit"
        return base

    def is_candidate(entry):
        u = entry.get("usage")
        return (entry["slot"] != active_slot and u and u.get("ok")
                and u.get("allowed") is not False
                and window_used(u, "primary") < LIMIT_EXHAUSTED_PERCENT
                and window_used(u, "secondary") < LIMIT_EXHAUSTED_PERCENT)

    candidates = [e for e in accounts if is_candidate(e)]
    if not candidates:
        base["reason"] = "no_candidate"
        return base
    candidates.sort(key=lambda e: (
        window_used(e["usage"], "primary"),
        window_used(e["usage"], "secondary"),
    ))
    target = candidates[0]

    reason = "rate_limit_blocked"
    if not blocked:
        reason = "weekly_limit_exhausted" if secondary_exhausted else "primary_limit_exhausted"
    base.update({
        "from_slot": active_slot,
        "to_slot": target["slot"],
        "to_email": target["email"],
        "reason": reason,
    })
    return base


def _coerce_config_value(key, value):
    if key in ("autoswitch_enabled", "notifications"):
        lowered = value.strip().lower()
        if lowered in ("true", "1", "yes", "on"):
            return True
        if lowered in ("false", "0", "no", "off"):
            return False
        raise ValueError("%s expects a boolean (true/false)" % key)
    if key == "tray_display":
        lowered = value.strip().lower()
        if lowered in ("both", "codex", "antigravity"):
            return lowered
        raise ValueError("%s expects both/codex/antigravity" % key)
    if key in ("antigravity_tray_target", "antigravity_target"):
        lowered = value.strip().lower()
        if lowered in ("both", "all"):
            return "both"
        if lowered in ("cli", "agent", "agent_cli"):
            return "cli"
        if lowered in ("ide",):
            return "ide"
        raise ValueError("%s expects both/cli/ide" % key)
    if key in ("antigravity_tray_models", "antigravity_tray_display"):
        lowered = value.strip().lower()
        if lowered in ("both", "all"):
            return "both"
        if lowered in ("gemini",):
            return "gemini"
        if lowered in ("claude_gpt", "claude-gpt", "claude", "gpt", "third_party", "thirdparty", "3p"):
            return "claude_gpt"
        raise ValueError("%s expects both/gemini/claude_gpt" % key)
    if key == "tray_slots":
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
                if isinstance(parsed, list):
                    value = parsed
                else:
                    value = [s.strip() for s in value.split(",") if s.strip()]
            except Exception:
                value = [s.strip() for s in value.split(",") if s.strip()]
        if isinstance(value, list):
            valid = {"codex", "ag_cli_gemini", "ag_cli_claude", "ag_ide_gemini", "ag_ide_claude"}
            expanded = []
            for raw in value:
                token = str(raw).strip().lower()
                if token == "ag_cli":
                    expanded.extend(["ag_cli_gemini", "ag_cli_claude"])
                elif token == "ag_ide":
                    expanded.extend(["ag_ide_gemini", "ag_ide_claude"])
                elif token in valid:
                    expanded.append(token)
            return list(dict.fromkeys(expanded))
        raise ValueError("%s expects a list of slot IDs" % key)
    if key == "client_id":
        return str(value)
    raise ValueError("unknown config key: %s" % key)


def cmd_config(args):
    cfg = load_config()
    if not args or args[0] == "get":
        return {"ok": True, "config": cfg}
    if args[0] == "set" and len(args) >= 3:
        key, value = args[1], " ".join(args[2:])
        try:
            cfg[key] = _coerce_config_value(key, value)
        except ValueError as exc:
            return {"ok": False, "error": str(exc), "config": cfg}
        save_config(cfg)
        return {"ok": True, "config": cfg}
    return {"ok": False, "error": "usage: config get | config set <key> <value>",
            "config": cfg}


# --------------------------------------------------------------------------
# Re-authentication (interactive browser OAuth via `codex login`)
# --------------------------------------------------------------------------

def cmd_relogin(args):
    """Re-authenticate one slot via an *isolated* `codex login`, then save the
    fresh credentials into that slot only.

    The login runs with CODEX_HOME pointed at a private temp directory, so the
    real ~/.codex/auth.json is never touched: the active account keeps running
    uninterrupted, and — crucially — the Codex desktop app can never grab and
    re-rotate (and thereby invalidate) the freshly issued tokens mid-login.
    That re-rotation, caused by the old approach of swapping auth.json, was the
    "fix one slot, the next one dies" loop. This switches nothing.
    """
    base = {"ok": False, "slot": None, "email": None, "error": None, "log": []}
    try:
        slot_number = int(args[0])
    except (IndexError, ValueError):
        base["error"] = "usage: relogin <slot>"
        return base
    base["slot"] = slot_number

    target = next((s for s in discover_slots() if s["slot"] == slot_number), None)
    if target is None:
        base["error"] = "no such slot: %d" % slot_number
        return base

    expected_email = email_from_tokens(target["tokens"])
    expected_id = target["account_id"]
    base["email"] = expected_email
    log = []

    # Private, isolated CODEX_HOME for this login only.
    tmp_home = KS_DIR / ("relogin_%d" % slot_number)
    try:
        shutil.rmtree(tmp_home, ignore_errors=True)
        tmp_home.mkdir(parents=True, exist_ok=True)
        os.chmod(tmp_home, 0o700)
        # Carry config.toml so `codex login` does not fail loading configuration.
        src_cfg = CODEX_DIR / "config.toml"
        if src_cfg.exists():
            shutil.copy2(src_cfg, tmp_home / "config.toml")
    except Exception as exc:
        shutil.rmtree(tmp_home, ignore_errors=True)
        base["error"] = "could not prepare isolated login: %s" % exc
        return base

    # Launch the interactive OAuth login (opens the browser). Blocking. The
    # tokens land in tmp_home/auth.json, never in the real ~/.codex/auth.json.
    pidfile = KS_DIR / ("relogin_%d.pid" % slot_number)
    try:
        returncode, stdout = run_codex_login(tmp_home, pidfile=pidfile)
    except FileNotFoundError:
        shutil.rmtree(tmp_home, ignore_errors=True)
        base["error"] = "codex CLI not found"
        return base
    except subprocess.TimeoutExpired:
        shutil.rmtree(tmp_home, ignore_errors=True)
        base["error"] = "login timed out (no sign-in within %d s)" % RELOGIN_TIMEOUT
        return base
    if stdout:
        log.extend(line for line in stdout.splitlines() if line.strip())
    if returncode != 0:
        shutil.rmtree(tmp_home, ignore_errors=True)
        base["error"] = "codex login failed (exit %d)" % returncode
        base["log"] = log
        return base

    # Read the freshly issued credentials from the isolated home, then wipe it.
    fresh_data = read_json(tmp_home / "auth.json")
    new_tokens = (fresh_data or {}).get("tokens") or {}
    new_email = email_from_tokens(new_tokens)
    new_id = new_tokens.get("account_id")
    shutil.rmtree(tmp_home, ignore_errors=True)

    if not isinstance(fresh_data, dict) or not new_tokens.get("access_token") or not new_id:
        base["error"] = "login produced no valid credentials"
        base["log"] = log
        return base

    # Guard: did they sign in under the account this slot expects?
    if expected_id and new_id != expected_id:
        base["error"] = ("signed in as %s, but slot %d is %s — slot left unchanged"
                         % (new_email or "another account", slot_number,
                            expected_email or "a different account"))
        base["log"] = log
        return base

    # Persist the fresh credentials into the slot (atomic, 0600). The active
    # auth.json is untouched, so nothing about the running session changes.
    write_json_atomic(target["path"], fresh_data, 0o600)
    # Clear any prior refresh-failure cooldown for this account.
    try:
        state = load_state()
        failures = state.get("refresh_failures") or {}
        if new_id and new_id in failures:
            failures.pop(new_id, None)
            state["refresh_failures"] = failures
            save_state(state)
    except Exception:
        pass

    base.update({
        "ok": True,
        "email": new_email or expected_email,
        "stayed_active": True,
        "log": log,
    })
    return base


def cmd_add(args):
    """Create a new account slot by running an isolated `codex login` (browser OAuth)."""
    base = {"ok": False, "slot": None, "email": None, "error": None, "log": []}

    slots = discover_slots()
    existing_slots = {s["slot"] for s in slots}
    slot_number = 1
    while slot_number in existing_slots:
        slot_number += 1

    base["slot"] = slot_number
    log = []

    # Private, isolated CODEX_HOME for this login only.
    tmp_home = KS_DIR / ("add_%d" % slot_number)
    try:
        shutil.rmtree(tmp_home, ignore_errors=True)
        tmp_home.mkdir(parents=True, exist_ok=True)
        os.chmod(tmp_home, 0o700)
        # Carry config.toml so `codex login` does not fail loading configuration.
        src_cfg = CODEX_DIR / "config.toml"
        if src_cfg.exists():
            shutil.copy2(src_cfg, tmp_home / "config.toml")
    except Exception as exc:
        shutil.rmtree(tmp_home, ignore_errors=True)
        base["error"] = "could not prepare isolated login: %s" % exc
        return base

    # Launch the interactive OAuth login (opens the browser). Blocking.
    pidfile = KS_DIR / "add.pid"
    try:
        returncode, stdout = run_codex_login(tmp_home, pidfile=pidfile)
    except FileNotFoundError:
        shutil.rmtree(tmp_home, ignore_errors=True)
        base["error"] = "codex CLI not found"
        return base
    except subprocess.TimeoutExpired:
        shutil.rmtree(tmp_home, ignore_errors=True)
        base["error"] = "login timed out (no sign-in within %d s)" % RELOGIN_TIMEOUT
        return base
    if stdout:
        log.extend(line for line in stdout.splitlines() if line.strip())
    if returncode != 0:
        shutil.rmtree(tmp_home, ignore_errors=True)
        base["error"] = "codex login failed (exit %d)" % returncode
        base["log"] = log
        return base

    # Read the freshly issued credentials from the isolated home, then wipe it.
    fresh_data = read_json(tmp_home / "auth.json")
    new_tokens = (fresh_data or {}).get("tokens") or {}
    new_email = email_from_tokens(new_tokens)
    new_id = new_tokens.get("account_id")
    shutil.rmtree(tmp_home, ignore_errors=True)

    if not isinstance(fresh_data, dict) or not new_tokens.get("access_token") or not new_id:
        base["error"] = "login produced no valid credentials"
        base["log"] = log
        return base

    # Guard: check if this account already exists in another slot
    for s in slots:
        if s["account_id"] == new_id:
            base["error"] = "account %s already exists in slot %d" % (new_email or new_id, s["slot"])
            base["log"] = log
            return base

    # Save to ~/.codex/accounts/auth_N.json
    ACCOUNTS_DIR.mkdir(parents=True, exist_ok=True)
    slot_path = ACCOUNTS_DIR / ("auth_%d.json" % slot_number)
    write_json_atomic(slot_path, fresh_data, 0o600)

    base.update({
        "ok": True,
        "email": new_email,
        "log": log,
    })
    return base


def cmd_delete(args):
    """Delete an account slot. If the deleted account is active, also clear active credentials."""
    base = {"ok": False, "error": None}
    try:
        slot_number = int(args[0])
    except (IndexError, ValueError):
        base["error"] = "usage: delete <slot>"
        return base

    slot_path = ACCOUNTS_DIR / ("auth_%d.json" % slot_number)
    if not slot_path.exists():
        base["error"] = "no such slot: %d" % slot_number
        return base

    try:
        # Check if the deleted slot is active
        data = read_json(slot_path)
        deleted_id = ((data or {}).get("tokens") or {}).get("account_id")

        # Delete the slot file
        slot_path.unlink()

        # If it was active, back up then delete ~/.codex/auth.json as well
        if deleted_id:
            active_data = read_json(AUTH_FILE)
            active_id = ((active_data or {}).get("tokens") or {}).get("account_id")
            if active_id == deleted_id and AUTH_FILE.exists():
                shutil.copy2(AUTH_FILE, CODEX_DIR / "auth.json.bak")
                AUTH_FILE.unlink()
        
        base["ok"] = True
    except Exception as exc:
        base["error"] = "failed to delete slot %d: %s" % (slot_number, exc)
    return base


def cmd_cancel_add(_args):
    """Cancel a running interactive add/relogin browser login."""
    return {"ok": True, "stopped": kill_pending_logins()}


def cmd_antigravity(args):
    return antigravity_engine.main(args)


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

COMMANDS = {
    "status": cmd_status,
    "switch": cmd_switch,
    "autoswitch-check": cmd_autoswitch_check,
    "relogin": cmd_relogin,
    "add": cmd_add,
    "cancel-add": cmd_cancel_add,
    "delete": cmd_delete,
    "config": cmd_config,
    "antigravity": cmd_antigravity,
}


def main(argv):
    KS_DIR.mkdir(parents=True, exist_ok=True)
    command = argv[1] if len(argv) > 1 else ""
    handler = COMMANDS.get(command)
    if handler is None:
        return {"ok": False,
                "error": "usage: keyswitcher.py <status|switch|autoswitch-check|relogin|add|cancel-add|delete|config|antigravity> [args]"}
    return handler(argv[2:])


if __name__ == "__main__":
    try:
        print(json.dumps(main(sys.argv)))
        sys.exit(0)
    except Exception as exc:  # catastrophic: still emit one JSON object
        print("keyswitcher: %s: %s" % (type(exc).__name__, exc), file=sys.stderr)
        print(json.dumps({"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}))
        sys.exit(1)
