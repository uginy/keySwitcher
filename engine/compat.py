#!/usr/bin/env python3
"""OS compatibility helpers for the KeySwitcher engine.

macOS behavior stays identical to the original tree. Windows gets file locks,
Credential Manager, process control, and the usual AppData/LocalAppData paths.
Stdlib only.
"""

import contextlib
import ctypes
import os
import shutil
import subprocess
import sys
import webbrowser
from pathlib import Path

IS_WIN = sys.platform == "win32"
HOME = Path.home()

CODEX_STORE_PATH_HINT = "OpenAI.Codex_"
CODEX_AUMID = "OpenAI.Codex_2p2nqsd0c76g0!App"
ANTIGRAVITY_AUMID = "Google.Antigravity"
USER_AGENT_PLATFORM = "windows/amd64" if IS_WIN else "darwin/arm64"


def appdata_dir():
    if IS_WIN:
        return Path(os.environ.get("APPDATA") or (HOME / "AppData" / "Roaming"))
    return HOME / "Library" / "Application Support"


def localappdata_dir():
    if IS_WIN:
        return Path(os.environ.get("LOCALAPPDATA") or (HOME / "AppData" / "Local"))
    return HOME / "Library"


def keyswitcher_support_dir():
    return appdata_dir() / "KeySwitcher"


def keyswitcher_log_dir():
    if IS_WIN:
        return localappdata_dir() / "KeySwitcher" / "Logs"
    return HOME / "Library" / "Logs" / "KeySwitcher"


def antigravity_default_root():
    return keyswitcher_support_dir() / "Antigravity"


def antigravity_ide_db():
    if IS_WIN:
        return appdata_dir() / "Antigravity" / "User" / "globalStorage" / "state.vscdb"
    return HOME / "Library" / "Application Support" / "Antigravity IDE" / "User" / "globalStorage" / "state.vscdb"


def antigravity_ide_app():
    if IS_WIN:
        return localappdata_dir() / "Programs" / "Antigravity"
    return Path("/Applications/Antigravity IDE.app")


def antigravity_shared_app():
    if IS_WIN:
        return localappdata_dir() / "Programs" / "Antigravity"
    return Path("/Applications/Antigravity.app")


def antigravity_ide_exe():
    if IS_WIN:
        return antigravity_ide_app() / "Antigravity.exe"
    return antigravity_ide_app()


def official_auth_binaries():
    if IS_WIN:
        root = antigravity_ide_app()
        return (
            root / "resources" / "app" / "out" / "main.js",
            root / "resources" / "app" / "extensions" / "antigravity" / "bin" / "language_server_windows_x64.exe",
        )
    return (
        Path("/Applications/Antigravity IDE.app/Contents/Resources/app/out/main.js"),
        Path("/Applications/Antigravity.app/Contents/Resources/bin/language_server"),
        Path("/Applications/Antigravity IDE.app/Contents/Resources/app/extensions/antigravity/bin/language_server_macos_arm"),
    )


def antigravity_user_agent(version="2.3.1"):
    return "antigravity/%s %s" % (version, USER_AGENT_PLATFORM)


def codex_log_dir():
    if IS_WIN:
        return localappdata_dir() / "Codex" / "Logs"
    return HOME / "Library" / "Logs" / "com.openai.codex"


def launch_agent_plist():
    return HOME / "Library" / "LaunchAgents" / "com.codex.rotator.plist"


def secure_chmod(path, mode):
    with contextlib.suppress(OSError):
        os.chmod(path, mode)


def is_runnable_file(path):
    path = Path(path)
    if not path.is_file():
        return False
    if os.access(path, os.X_OK):
        return True
    if IS_WIN and path.suffix.lower() in (".exe", ".cmd", ".bat", ".ps1", ".com"):
        return True
    return False


def unique_existing_runnables(candidates):
    unique = []
    for cand in candidates:
        path = Path(cand) if cand else None
        if path and is_runnable_file(path) and path not in unique:
            unique.append(path)
    return unique


def open_url(url):
    try:
        if webbrowser.open(url, new=1, autoraise=True):
            return True
    except Exception:
        pass
    if IS_WIN:
        try:
            os.startfile(url)  # noqa: S606
            return True
        except OSError:
            return False
    try:
        subprocess.run(
            ["open", url],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=True,
        )
        return True
    except Exception:
        return False


def popen_detached(args):
    kwargs = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if IS_WIN:
        flags = subprocess.CREATE_NEW_PROCESS_GROUP
        if hasattr(subprocess, "DETACHED_PROCESS"):
            flags |= subprocess.DETACHED_PROCESS
        if hasattr(subprocess, "CREATE_NO_WINDOW"):
            flags |= subprocess.CREATE_NO_WINDOW
        kwargs["creationflags"] = flags
    else:
        kwargs["start_new_session"] = True
    return subprocess.Popen(args, **kwargs)


def terminate_pid(pid, graceful=True):
    if not pid:
        return
    if IS_WIN:
        # SIGTERM maps to TerminateProcess on Windows.
        with contextlib.suppress(OSError):
            os.kill(int(pid), 15)
        return
    import signal
    with contextlib.suppress(OSError):
        os.kill(int(pid), signal.SIGTERM if graceful else signal.SIGKILL)


# --------------------------------------------------------------------------
# Advisory lock
# --------------------------------------------------------------------------

if IS_WIN:
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    class _OVERLAPPED(ctypes.Structure):
        _fields_ = [
            ("Internal", ctypes.c_ulonglong),
            ("InternalHigh", ctypes.c_ulonglong),
            ("Offset", ctypes.c_uint32),
            ("OffsetHigh", ctypes.c_uint32),
            ("hEvent", ctypes.c_void_p),
        ]

    _LockFileEx = _kernel32.LockFileEx
    _LockFileEx.argtypes = [
        ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32,
        ctypes.c_uint32, ctypes.c_uint32, ctypes.POINTER(_OVERLAPPED),
    ]
    _LockFileEx.restype = ctypes.c_int
    _UnlockFileEx = _kernel32.UnlockFileEx
    _UnlockFileEx.argtypes = [
        ctypes.c_void_p, ctypes.c_uint32,
        ctypes.c_uint32, ctypes.c_uint32, ctypes.POINTER(_OVERLAPPED),
    ]
    _UnlockFileEx.restype = ctypes.c_int
    _LOCKFILE_EXCLUSIVE_LOCK = 0x00000002

    def _win_lock(fh):
        handle = msvcrt.get_osfhandle(fh.fileno())
        overlapped = _OVERLAPPED()
        if not _LockFileEx(handle, _LOCKFILE_EXCLUSIVE_LOCK, 0, 1, 0, ctypes.byref(overlapped)):
            raise OSError(ctypes.get_last_error(), "LockFileEx failed")

    def _win_unlock(fh):
        handle = msvcrt.get_osfhandle(fh.fileno())
        overlapped = _OVERLAPPED()
        _UnlockFileEx(handle, 0, 1, 0, ctypes.byref(overlapped))

    import msvcrt
else:
    import fcntl


@contextlib.contextmanager
def exclusive_lock(lock_file):
    lock_file = Path(lock_file)
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    fh = open(lock_file, "a+b")
    try:
        secure_chmod(lock_file, 0o600)
        if IS_WIN:
            _win_lock(fh)
        else:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        if IS_WIN:
            with contextlib.suppress(OSError):
                _win_unlock(fh)
        else:
            with contextlib.suppress(OSError):
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        fh.close()


# --------------------------------------------------------------------------
# Process helpers
# --------------------------------------------------------------------------

def _win_iter_processes():
    """Yield (pid, name, exe_path) for running processes. Windows only."""
    TH32CS_SNAPPROCESS = 0x00000002
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", ctypes.c_uint32),
            ("cntUsage", ctypes.c_uint32),
            ("th32ProcessID", ctypes.c_uint32),
            ("th32DefaultHeapID", ctypes.c_void_p),
            ("th32ModuleID", ctypes.c_uint32),
            ("cntThreads", ctypes.c_uint32),
            ("th32ParentProcessID", ctypes.c_uint32),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", ctypes.c_uint32),
            ("szExeFile", ctypes.c_wchar * 260),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snapshot == ctypes.c_void_p(-1).value:
        return
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        if not kernel32.Process32FirstW(snapshot, ctypes.byref(entry)):
            return
        while True:
            pid = entry.th32ProcessID
            name = entry.szExeFile
            exe = ""
            handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if handle:
                try:
                    buf = ctypes.create_unicode_buffer(32768)
                    size = ctypes.c_uint32(len(buf))
                    if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                        exe = buf.value
                finally:
                    kernel32.CloseHandle(handle)
            yield pid, name, exe
            if not kernel32.Process32NextW(snapshot, ctypes.byref(entry)):
                break
    finally:
        kernel32.CloseHandle(snapshot)


def process_name_running(names):
    wanted = {name.lower() for name in names}
    if IS_WIN:
        for _pid, name, _exe in _win_iter_processes():
            if name.lower() in wanted:
                return True
        return False
    for name in names:
        result = subprocess.run(
            ["pgrep", "-x", name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if result.returncode == 0:
            return True
    return False


def processes_matching(names=None, path_contains=None):
    """Return [(pid, name, exe)] filtered by image name and/or path substring."""
    names = {n.lower() for n in (names or [])}
    needles = [s.lower() for s in (path_contains or [])]
    found = []
    if IS_WIN:
        for pid, name, exe in _win_iter_processes():
            if names and name.lower() not in names:
                continue
            if needles and not any(n in (exe or "").lower() for n in needles):
                continue
            found.append((pid, name, exe))
        return found
    return found


def command_running(pattern):
    """True if a process command line / image path contains pattern."""
    if IS_WIN:
        needle = pattern.lower()
        for _pid, name, exe in _win_iter_processes():
            if needle in (exe or "").lower() or needle in name.lower():
                return True
        return False
    try:
        result = subprocess.run(
            ["pgrep", "-f", pattern],
            capture_output=True,
            timeout=10,
        )
        return result.returncode == 0
    except Exception:
        return False


def osascript_app_running(bundle_id):
    result = subprocess.run(
        ["osascript", "-e", 'application id "%s" is running' % bundle_id],
        capture_output=True, text=True, timeout=5,
    )
    return result.returncode == 0 and result.stdout.strip().lower() == "true"


def osascript_quit(bundle_id):
    subprocess.run(
        ["osascript", "-e", 'tell application id "%s" to quit' % bundle_id],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10,
    )


def open_bundle(bundle_id):
    result = subprocess.run(
        ["open", "-b", bundle_id],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=10,
    )
    return result.returncode == 0


def launch_aumid(aumid):
    if not IS_WIN:
        return False
    try:
        subprocess.Popen(
            ["explorer.exe", "shell:AppsFolder\\" + aumid],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return True
    except OSError:
        return False


def launch_exe(path):
    path = Path(path)
    if not path.is_file():
        return False
    try:
        subprocess.Popen(
            [str(path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            cwd=str(path.parent),
        )
        return True
    except OSError:
        return False


def terminate_matching(names=None, path_contains=None):
    if not IS_WIN:
        return False
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    PROCESS_TERMINATE = 0x0001
    killed = False
    for pid, _name, _exe in processes_matching(names=names, path_contains=path_contains):
        handle = kernel32.OpenProcess(PROCESS_TERMINATE, False, pid)
        if not handle:
            continue
        try:
            if kernel32.TerminateProcess(handle, 1):
                killed = True
        finally:
            kernel32.CloseHandle(handle)
    return killed


def wait_until_gone(predicate, timeout_s=6.0, interval=0.25):
    import time
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if not predicate():
            return True
        time.sleep(interval)
    return not predicate()


# --------------------------------------------------------------------------
# Codex desktop
# --------------------------------------------------------------------------

def codex_desktop_running():
    if IS_WIN:
        if processes_matching(names=["ChatGPT.exe", "Codex.exe"], path_contains=[CODEX_STORE_PATH_HINT]):
            return True
        if processes_matching(names=["Codex.exe"]):
            return True
        return False
    return process_name_running(("Codex", "ChatGPT"))


def quit_codex_desktop():
    if not IS_WIN:
        if not codex_desktop_running():
            return
        osascript_quit("com.openai.codex")
        if wait_until_gone(codex_desktop_running, timeout_s=6.0):
            return
        subprocess.run(
            ["killall", "Codex"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return
    if not codex_desktop_running():
        return
    terminate_matching(names=["ChatGPT.exe", "Codex.exe"], path_contains=[CODEX_STORE_PATH_HINT])
    terminate_matching(names=["Codex.exe"], path_contains=["\\Codex\\", "/Codex/"])
    wait_until_gone(codex_desktop_running, timeout_s=4.0)


def relaunch_codex_desktop():
    if IS_WIN:
        if launch_aumid(CODEX_AUMID):
            return True
        store = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "WindowsApps"
        if store.is_dir():
            matches = sorted(store.glob("OpenAI.Codex_*\\app\\ChatGPT.exe"))
            if matches:
                return launch_exe(matches[-1])
        return False
    return open_bundle("com.openai.codex")


def reopen_codex_thread(thread_id):
    if not thread_id:
        return False
    return open_url("codex://threads/%s" % thread_id)


# --------------------------------------------------------------------------
# Antigravity desktop
# --------------------------------------------------------------------------

def antigravity_process_running():
    if IS_WIN:
        return bool(processes_matching(names=["Antigravity.exe"]))
    return False


def antigravity_app_running(bundle_id):
    if os.environ.get("KEYSWITCHER_ANTIGRAVITY_SKIP_APP_CONTROL") == "1":
        return False
    if IS_WIN:
        return antigravity_process_running()
    return osascript_app_running(bundle_id)


def stop_antigravity_app(bundle_id):
    if os.environ.get("KEYSWITCHER_ANTIGRAVITY_SKIP_APP_CONTROL") == "1":
        return
    if IS_WIN:
        terminate_matching(names=["Antigravity.exe"])
        if not wait_until_gone(antigravity_process_running, timeout_s=6.0):
            raise RuntimeError("Close the target Antigravity app and try again")
        return
    osascript_quit(bundle_id)
    if not wait_until_gone(lambda: osascript_app_running(bundle_id), timeout_s=6.0):
        raise RuntimeError("Close the target Antigravity app and try again")


def open_antigravity_app(bundle_id, exe_path=None):
    if os.environ.get("KEYSWITCHER_ANTIGRAVITY_SKIP_APP_CONTROL") == "1":
        return
    if IS_WIN:
        exe = Path(exe_path) if exe_path else antigravity_ide_exe()
        if exe.is_file() and launch_exe(exe):
            return
        if launch_aumid(ANTIGRAVITY_AUMID):
            return
        raise RuntimeError("Could not open the target Antigravity app")
    if not open_bundle(bundle_id):
        raise RuntimeError("Could not open the target Antigravity app")


# --------------------------------------------------------------------------
# Codex CLI discovery extras
# --------------------------------------------------------------------------

def extra_codex_path_dirs():
    dirs = [
        HOME / ".local" / "bin",
        HOME / ".bun" / "bin",
    ]
    if IS_WIN:
        dirs.extend([
            appdata_dir() / "npm",
            HOME / "AppData" / "Roaming" / "npm",
            localappdata_dir() / "OpenAI" / "Codex" / "bin",
        ])
    else:
        dirs.extend([
            Path("/opt/homebrew/bin"),
            Path("/usr/local/bin"),
        ])
    return dirs


def extra_codex_glob_roots():
    if not IS_WIN:
        return []
    return [
        appdata_dir() / "npm" / "node_modules",
        HOME / "AppData" / "Roaming" / "npm" / "node_modules",
        localappdata_dir() / "OpenAI" / "Codex",
    ]


WINDOWS_CODEX_GLOBS = [
    "@openai/codex/node_modules/@openai/codex-*/vendor/*/bin/codex.exe",
    "@openai/codex-*/vendor/*/bin/codex.exe",
    "node_modules/@openai/codex-*/vendor/*/bin/codex.exe",
    "**/codex.exe",
]


def extra_cli_candidates():
    if not IS_WIN:
        return []
    return [
        shutil.which("codex.cmd"),
        shutil.which("codex.exe"),
        str(appdata_dir() / "npm" / "codex.cmd"),
        str(appdata_dir() / "npm" / "codex.CMD"),
    ]


# --------------------------------------------------------------------------
# Windows Credential Manager (generic credentials)
# --------------------------------------------------------------------------

if IS_WIN:
    _advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    _CRED_TYPE_GENERIC = 1
    _CRED_PERSIST_LOCAL_MACHINE = 2

    class _FILETIME(ctypes.Structure):
        _fields_ = [("dwLowDateTime", ctypes.c_uint32), ("dwHighDateTime", ctypes.c_uint32)]

    class _CREDENTIALW(ctypes.Structure):
        _fields_ = [
            ("Flags", ctypes.c_uint32),
            ("Type", ctypes.c_uint32),
            ("TargetName", ctypes.c_wchar_p),
            ("Comment", ctypes.c_wchar_p),
            ("LastWritten", _FILETIME),
            ("CredentialBlobSize", ctypes.c_uint32),
            ("CredentialBlob", ctypes.c_void_p),
            ("Persist", ctypes.c_uint32),
            ("AttributeCount", ctypes.c_uint32),
            ("Attributes", ctypes.c_void_p),
            ("TargetAlias", ctypes.c_wchar_p),
            ("UserName", ctypes.c_wchar_p),
        ]

    _CredReadW = _advapi32.CredReadW
    _CredReadW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.POINTER(ctypes.POINTER(_CREDENTIALW))]
    _CredReadW.restype = ctypes.c_int
    _CredWriteW = _advapi32.CredWriteW
    _CredWriteW.argtypes = [ctypes.POINTER(_CREDENTIALW), ctypes.c_uint32]
    _CredWriteW.restype = ctypes.c_int
    _CredDeleteW = _advapi32.CredDeleteW
    _CredDeleteW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32]
    _CredDeleteW.restype = ctypes.c_int
    _CredFree = _advapi32.CredFree
    _CredFree.argtypes = [ctypes.c_void_p]
    _CredFree.restype = None

    def _cred_targets(service, account):
        return [
            "%s:%s" % (service, account),
            "%s/%s" % (service, account),
            service,
        ]

    def wincred_get(service, account):
        last_missing = False
        for target in _cred_targets(service, account):
            cred_ptr = ctypes.POINTER(_CREDENTIALW)()
            if not _CredReadW(target, _CRED_TYPE_GENERIC, 0, ctypes.byref(cred_ptr)):
                err = ctypes.get_last_error()
                if err in (1168, 0):  # ERROR_NOT_FOUND
                    last_missing = True
                    continue
                raise OSError(err, "CredRead failed for %s" % target)
            try:
                cred = cred_ptr.contents
                size = cred.CredentialBlobSize
                blob = ctypes.string_at(cred.CredentialBlob, size) if size and cred.CredentialBlob else b""
                user = cred.UserName or ""
                if target == service and account and user and user != account:
                    continue
                return blob
            finally:
                _CredFree(cred_ptr)
        if last_missing:
            return None
        return None

    def wincred_set(service, account, value):
        if not value:
            raise RuntimeError("Refusing to save an empty credential")
        target = "%s:%s" % (service, account)
        blob = value if isinstance(value, (bytes, bytearray)) else value.encode("utf-8")
        buf = ctypes.create_string_buffer(bytes(blob))
        cred = _CREDENTIALW()
        cred.Type = _CRED_TYPE_GENERIC
        cred.TargetName = target
        cred.CredentialBlobSize = len(blob)
        cred.CredentialBlob = ctypes.cast(buf, ctypes.c_void_p)
        cred.Persist = _CRED_PERSIST_LOCAL_MACHINE
        cred.UserName = account
        if not _CredWriteW(ctypes.byref(cred), 0):
            raise OSError(ctypes.get_last_error(), "CredWrite failed")

    def wincred_delete(service, account):
        missing = True
        last_err = 0
        for target in _cred_targets(service, account):
            if _CredDeleteW(target, _CRED_TYPE_GENERIC, 0):
                missing = False
                continue
            err = ctypes.get_last_error()
            if err in (1168, 0):
                continue
            last_err = err
        if last_err:
            raise OSError(last_err, "CredDelete failed")
        return not missing
else:
    def wincred_get(service, account):
        raise RuntimeError("Windows Credential Manager is not available")

    def wincred_set(service, account, value):
        raise RuntimeError("Windows Credential Manager is not available")

    def wincred_delete(service, account):
        raise RuntimeError("Windows Credential Manager is not available")


def helper_command(helper_path):
    """Build argv for the Keychain/credential helper, including shebang scripts."""
    helper_path = Path(helper_path)
    argv = [str(helper_path)]
    if not IS_WIN:
        return argv
    try:
        first = helper_path.read_bytes().splitlines()[:1]
    except OSError:
        first = []
    if first and first[0].startswith(b"#!") and b"python" in first[0].lower():
        return [sys.executable, str(helper_path)]
    if helper_path.suffix.lower() == ".py":
        return [sys.executable, str(helper_path)]
    return argv
