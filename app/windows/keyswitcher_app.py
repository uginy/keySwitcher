#!/usr/bin/env python3
"""KeySwitcher Windows tray frontend.

Talks to engine/keyswitcher.py the same way the macOS Swift app does:
one JSON object per invocation, tokens never shown. Stdlib only.
"""

from __future__ import annotations

import contextlib
import ctypes
import json
import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from ctypes import wintypes
from pathlib import Path

NID_TIP_MAX = 127
POLL_MS = 60_000
ENGINE_TIMEOUT = {
    "config": 20,
    "delete": 20,
    "status": 60,
    "switch": 60,
    "antigravity": 60,
    "relogin": 330,
    "add": 330,
}

BG = "#1c1c1e"
BG_CARD = "#2c2c2e"
BG_CARD_ACTIVE = "#3a3a3c"
FG = "#f2f2f7"
FG_DIM = "#8e8e93"
GREEN = "#30d158"
YELLOW = "#ffd60a"
RED = "#ff453a"
BLUE = "#0a84ff"
ACCENT = "#64d2ff"


def _app_language_default():
    lang = os.environ.get("LANG") or os.environ.get("LANGUAGE") or ""
    if lang.lower().startswith("ru"):
        return "ru"
    try:
        import locale
        loc = (locale.getlocale()[0] or "").lower()
        if loc.startswith("ru"):
            return "ru"
    except Exception:
        pass
    return "en"


class L10n:
    def __init__(self):
        self.mode = "system"
        self.resolved = _app_language_default()

    def set_mode(self, mode):
        self.mode = mode
        self.resolved = "ru" if mode == "ru" else "en" if mode == "en" else _app_language_default()

    @property
    def ru(self):
        return self.resolved == "ru"

    def t(self, en, ru):
        return ru if self.ru else en


L = L10n()


def project_root():
    here = Path(__file__).resolve()
    # app/windows/keyswitcher_app.py -> repo root
    return here.parents[2]


def resolve_engine():
    env = os.environ.get("KEYSWITCHER_ENGINE")
    if env and Path(env).is_file():
        return Path(env)
    installed = Path(os.environ.get("LOCALAPPDATA", "")) / "KeySwitcher" / "engine" / "keyswitcher.py"
    if installed.is_file():
        return installed
    bundled = project_root() / "engine" / "keyswitcher.py"
    if bundled.is_file():
        return bundled
    return None


def python_exe():
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe":
        candidate = exe.with_name("python.exe")
        if candidate.is_file():
            return str(candidate)
    return str(exe)


def run_engine(args, timeout=None):
    engine = resolve_engine()
    if engine is None:
        raise RuntimeError(L.t("Engine not found", "Движок не найден"))
    if timeout is None:
        timeout = ENGINE_TIMEOUT.get(args[0] if args else "", 60)
    flags = 0
    if sys.platform == "win32" and hasattr(subprocess, "CREATE_NO_WINDOW"):
        flags = subprocess.CREATE_NO_WINDOW
    result = subprocess.run(
        [python_exe(), str(engine), *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        creationflags=flags,
    )
    lines = [line for line in (result.stdout or "").splitlines() if line.strip()]
    if not lines:
        err = (result.stderr or "").strip() or "empty engine output"
        raise RuntimeError(err)
    return json.loads(lines[-1])


def mask_email(email):
    if not email or "@" not in email:
        return email or L.t("unknown", "неизвестно")
    name, domain = email.split("@", 1)
    if len(name) <= 2:
        hidden = "*" * len(name)
    else:
        hidden = name[:2] + "*" * max(3, len(name) - 2)
    return hidden + "@" + domain


def remaining(used):
    try:
        return max(0, min(100, int(round(100.0 - float(used)))))
    except (TypeError, ValueError):
        return None


def usage_color(rem, allowed=True):
    if allowed is False or rem is None:
        return RED
    if rem <= 15:
        return RED
    if rem <= 40:
        return YELLOW
    return GREEN


def format_reset(reset_at):
    if not reset_at:
        return ""
    seconds = int(reset_at) - int(time.time())
    if seconds <= 0:
        return L.t("resets soon", "сброс скоро")
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    parts = []
    if L.ru:
        if days:
            parts.append("%d д" % days)
        if hours:
            parts.append("%d ч" % hours)
        if minutes:
            parts.append("%d мин" % minutes)
        if not parts:
            return "сброс меньше чем через минуту"
        return "сброс через " + " ".join(parts)
    if days:
        parts.append("%dd" % days)
    if hours:
        parts.append("%dh" % hours)
    if minutes:
        parts.append("%dm" % minutes)
    if not parts:
        return "resets in less than a minute"
    return "resets in " + " ".join(parts)


def config_path():
    root = Path.home() / ".codex" / "keyswitcher"
    root.mkdir(parents=True, exist_ok=True)
    return root / "ui.json"


def load_ui_config():
    try:
        data = json.loads(config_path().read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {}


def save_ui_config(data):
    config_path().write_text(json.dumps(data, indent=2), encoding="utf-8")


# --------------------------------------------------------------------------
# Launch at login (HKCU Run)
# --------------------------------------------------------------------------

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_NAME = "KeySwitcher"


def launch_at_login_enabled():
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.QueryValueEx(key, RUN_NAME)
            return True
    except OSError:
        return False


def set_launch_at_login(enabled):
    import winreg
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
        if enabled:
            script = Path(__file__).resolve()
            value = '"%s" "%s"' % (pythonw_path(), script)
            winreg.SetValueEx(key, RUN_NAME, 0, winreg.REG_SZ, value)
        else:
            try:
                winreg.DeleteValue(key, RUN_NAME)
            except FileNotFoundError:
                pass


def pythonw_path():
    exe = Path(sys.executable)
    if exe.name.lower() == "python.exe":
        candidate = exe.with_name("pythonw.exe")
        if candidate.is_file():
            return str(candidate)
    return str(exe)


# --------------------------------------------------------------------------
# Tray icon (NotifyIcon)
# --------------------------------------------------------------------------

user32 = ctypes.WinDLL("user32", use_last_error=True)
shell32 = ctypes.WinDLL("shell32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

WM_TRAY = 0x8000 + 21
WM_LBUTTONUP = 0x0202
WM_RBUTTONUP = 0x0205
WM_COMMAND = 0x0111
WM_DESTROY = 0x0002
NIF_MESSAGE = 0x00000001
NIF_ICON = 0x00000002
NIF_TIP = 0x00000004
NIM_ADD = 0x00000000
NIM_MODIFY = 0x00000001
NIM_DELETE = 0x00000002
IDI_APPLICATION = 32512
IMAGE_ICON = 1
LR_DEFAULTSIZE = 0x00000040
LR_SHARED = 0x00008000

LRESULT = ctypes.c_ssize_t
WPARAM = ctypes.c_size_t
LPARAM = ctypes.c_ssize_t
WNDPROCTYPE = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, WPARAM, LPARAM)
user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, WPARAM, LPARAM]
user32.DefWindowProcW.restype = LRESULT
user32.PeekMessageW.argtypes = [ctypes.c_void_p, wintypes.HWND, wintypes.UINT, wintypes.UINT, wintypes.UINT]
user32.PeekMessageW.restype = wintypes.BOOL
user32.TranslateMessage.argtypes = [ctypes.c_void_p]
user32.DispatchMessageW.argtypes = [ctypes.c_void_p]
user32.DispatchMessageW.restype = LRESULT


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("hWnd", wintypes.HWND),
        ("uID", wintypes.UINT),
        ("uFlags", wintypes.UINT),
        ("uCallbackMessage", wintypes.UINT),
        ("hIcon", wintypes.HICON),
        ("szTip", ctypes.c_wchar * 128),
        ("dwState", wintypes.DWORD),
        ("dwStateMask", wintypes.DWORD),
        ("szInfo", ctypes.c_wchar * 256),
        ("uVersion", wintypes.UINT),
        ("szInfoTitle", ctypes.c_wchar * 64),
        ("dwInfoFlags", wintypes.DWORD),
        ("guidItem", ctypes.c_byte * 16),
        ("hBalloonIcon", wintypes.HICON),
    ]


class WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", wintypes.UINT),
        ("lpfnWndProc", WNDPROCTYPE),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
    ]


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


def _load_default_icon():
    return user32.LoadImageW(
        None, ctypes.c_wchar_p(IDI_APPLICATION), IMAGE_ICON, 0, 0, LR_SHARED | LR_DEFAULTSIZE
    )


class TrayIcon:
    def __init__(self, events):
        self.events = events
        self.hwnd = None
        self.nid = None
        self._wndproc = WNDPROCTYPE(self._wndproc_impl)
        self._class_name = "KeySwitcherTrayWnd"

    def _wndproc_impl(self, hwnd, msg, wparam, lparam):
        try:
            if msg == WM_TRAY:
                click = int(lparam) & 0xFFFF
                if click == WM_LBUTTONUP:
                    self.events.put(("toggle", None))
                elif click == WM_RBUTTONUP:
                    self.events.put(("menu", None))
                return 0
            if msg == WM_DESTROY:
                return 0
        except Exception:
            return 0
        try:
            return int(user32.DefWindowProcW(hwnd, msg, wparam, lparam) or 0)
        except Exception:
            return 0

    def create(self):
        hinstance = kernel32.GetModuleHandleW(None)
        wc = WNDCLASSW()
        wc.lpfnWndProc = self._wndproc
        wc.hInstance = hinstance
        wc.lpszClassName = self._class_name
        atom = user32.RegisterClassW(ctypes.byref(wc))
        if not atom:
            err = ctypes.get_last_error()
            if err != 1410:  # already registered
                raise ctypes.WinError(err)
        self.hwnd = user32.CreateWindowExW(
            0, self._class_name, "KeySwitcher", 0, 0, 0, 0, 0, None, None, hinstance, None
        )
        if not self.hwnd:
            raise ctypes.WinError(ctypes.get_last_error())
        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        nid.hWnd = self.hwnd
        nid.uID = 1
        nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
        nid.uCallbackMessage = WM_TRAY
        nid.hIcon = _load_default_icon()
        nid.szTip = "KeySwitcher"
        if not shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid)):
            raise ctypes.WinError(ctypes.get_last_error())
        self.nid = nid

    def set_tip(self, text):
        if not self.nid:
            return
        self.nid.szTip = (text or "KeySwitcher")[:NID_TIP_MAX]
        self.nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
        shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(self.nid))

    def destroy(self):
        if self.nid:
            shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self.nid))
            self.nid = None
        if self.hwnd:
            user32.DestroyWindow(self.hwnd)
            self.hwnd = None

    def pump_once(self):
        if not self.hwnd:
            return
        msg = wintypes.MSG()
        while user32.PeekMessageW(ctypes.byref(msg), self.hwnd, 0, 0, 1):
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))


# --------------------------------------------------------------------------
# UI widgets
# --------------------------------------------------------------------------

class UsageBar(tk.Frame):
    def __init__(self, master, label, rem, reset_at, allowed=True):
        super().__init__(master, bg=BG_CARD)
        color = usage_color(rem, allowed)
        text = "%s  %s" % (label, ("%d%%" % rem) if rem is not None else L.t("no data", "нет данных"))
        extra = format_reset(reset_at)
        if extra:
            text = "%s  ·  %s" % (text, extra)
        tk.Label(self, text=text, fg=FG_DIM, bg=BG_CARD, font=("Segoe UI", 8), anchor="w").pack(fill="x")
        canvas = tk.Canvas(self, height=6, bg=BG_CARD, highlightthickness=0, bd=0)
        canvas.pack(fill="x", pady=(2, 6))
        self.update_idletasks()
        width = max(master.winfo_reqwidth(), 220)

        def draw(_event=None):
            canvas.delete("all")
            w = canvas.winfo_width() or width
            canvas.create_rectangle(0, 0, w, 6, fill="#3a3a3c", width=0)
            frac = 0 if rem is None else max(0.0, min(1.0, rem / 100.0))
            canvas.create_rectangle(0, 0, int(w * frac), 6, fill=color, width=0)

        canvas.bind("<Configure>", draw)
        draw()


class Dashboard(tk.Toplevel):
    def __init__(self, app):
        super().__init__(app.root)
        self.app = app
        self.title("KeySwitcher")
        self.configure(bg=BG)
        self.resizable(False, False)
        self.protocol("WM_DELETE_WINDOW", self.withdraw)
        self.geometry("760x560")
        self.revealed = set()
        self._busy = False
        self._build()

    def _build(self):
        header = tk.Frame(self, bg=BG)
        header.pack(fill="x", padx=16, pady=(12, 8))
        tk.Label(header, text="KeySwitcher", fg=FG, bg=BG, font=("Segoe UI", 14, "bold")).pack(side="left")
        self.status_lbl = tk.Label(header, text="", fg=FG_DIM, bg=BG, font=("Segoe UI", 8))
        self.status_lbl.pack(side="right")

        body = tk.Frame(self, bg=BG)
        body.pack(fill="both", expand=True, padx=12, pady=4)
        self.left = tk.Frame(body, bg=BG)
        self.right = tk.Frame(body, bg=BG)
        self.left.pack(side="left", fill="both", expand=True, padx=(0, 6))
        self.right.pack(side="right", fill="both", expand=True, padx=(6, 0))

        footer = tk.Frame(self, bg=BG)
        footer.pack(fill="x", padx=16, pady=(4, 12))
        self._btn(footer, L.t("Refresh", "Обновить"), self.app.refresh).pack(side="left")
        self._btn(footer, L.t("Add Codex", "Добавить Codex"), self.app.add_codex).pack(side="left", padx=6)
        self._btn(footer, L.t("Add Antigravity", "Добавить Antigravity"), self.app.add_antigravity).pack(side="left")
        self._btn(footer, L.t("Quit", "Выйти"), self.app.quit_app).pack(side="right")
        self._btn(footer, L.t("Settings", "Настройки"), self._settings).pack(side="right", padx=6)

    def _btn(self, master, text, command, bg=BG_CARD, fg=FG):
        return tk.Button(
            master, text=text, command=command, bg=bg, fg=fg, activebackground="#48484a",
            activeforeground=FG, relief="flat", padx=10, pady=4, font=("Segoe UI", 9), cursor="hand2",
        )

    def set_status(self, text, error=False):
        self.status_lbl.configure(text=text or "", fg=RED if error else FG_DIM)

    def render(self, codex, antigravity):
        for frame in (self.left, self.right):
            for child in frame.winfo_children():
                child.destroy()
        self._section(self.left, "Codex", self._codex_cards(codex))
        self._section(self.right, "Antigravity", self._ag_cards(antigravity))

    def _section(self, parent, title, builder):
        tk.Label(parent, text=title, fg=ACCENT, bg=BG, font=("Segoe UI", 11, "bold"), anchor="w").pack(fill="x", pady=(0, 8))
        canvas = tk.Canvas(parent, bg=BG, highlightthickness=0)
        scroll = tk.Scrollbar(parent, orient="vertical", command=canvas.yview)
        inner = tk.Frame(canvas, bg=BG)
        inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=inner, anchor="nw", width=350)
        canvas.configure(yscrollcommand=scroll.set)
        canvas.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        builder(inner)

    def _card(self, parent, active=False):
        card = tk.Frame(parent, bg=BG_CARD_ACTIVE if active else BG_CARD, padx=10, pady=8)
        card.pack(fill="x", pady=4)
        return card

    def _codex_cards(self, status):
        def build(inner):
            if not status:
                tk.Label(inner, text=L.t("Loading…", "Загрузка…"), fg=FG_DIM, bg=BG).pack(anchor="w")
                return
            if status.get("ok") is False:
                tk.Label(inner, text=status.get("error") or L.t("Failed to load accounts", "Не удалось загрузить аккаунты"),
                         fg=RED, bg=BG, wraplength=320, justify="left").pack(anchor="w")
                return
            accounts = status.get("accounts") or []
            if not accounts:
                tk.Label(inner, text=L.t("No saved accounts yet", "Сохранённых аккаунтов пока нет"),
                         fg=FG_DIM, bg=BG).pack(anchor="w")
                return
            for acc in accounts:
                active = bool(acc.get("active"))
                card = self._card(inner, active)
                email = acc.get("email") or ("slot %s" % acc.get("slot"))
                shown = email if email in self.revealed else mask_email(email)
                top = tk.Frame(card, bg=card["bg"])
                top.pack(fill="x")
                mail = tk.Label(top, text=shown, fg=FG, bg=card["bg"], font=("Segoe UI", 10, "bold"), cursor="hand2")
                mail.pack(side="left")
                mail.bind("<Button-1>", lambda _e, e=email: self._toggle_email(e))
                plan = (acc.get("plan") or "").upper()
                if plan:
                    tk.Label(top, text=plan, fg=BLUE, bg=card["bg"], font=("Segoe UI", 8)).pack(side="right")
                if active:
                    tk.Label(card, text=L.t("Active", "Активен"), fg=GREEN, bg=card["bg"], font=("Segoe UI", 8)).pack(anchor="w")
                usage = acc.get("usage") or {}
                if acc.get("error") == "auth_expired":
                    tk.Label(card, text=L.t("session expired — login required", "сессия завершена — нужен повторный вход"),
                             fg=RED, bg=card["bg"], font=("Segoe UI", 8), wraplength=300, justify="left").pack(anchor="w")
                else:
                    primary = usage.get("primary") or {}
                    secondary = usage.get("secondary") or {}
                    UsageBar(card, L.t("5h", "5 ч"), remaining(primary.get("used_percent")),
                             primary.get("reset_at"), usage.get("allowed") is not False).pack(fill="x")
                    UsageBar(card, L.t("Week", "Неделя"), remaining(secondary.get("used_percent")),
                             secondary.get("reset_at"), usage.get("allowed") is not False).pack(fill="x")
                actions = tk.Frame(card, bg=card["bg"])
                actions.pack(fill="x", pady=(4, 0))
                if not active:
                    self._btn(actions, L.t("Switch", "Переключить"),
                              lambda s=acc["slot"]: self.app.switch_codex(s), bg=BLUE).pack(side="left")
                self._btn(actions, L.t("Re-login", "Войти снова"),
                          lambda s=acc["slot"]: self.app.relogin_codex(s)).pack(side="left", padx=4)
                self._btn(actions, L.t("Delete", "Удалить"),
                          lambda s=acc["slot"], e=email: self.app.delete_codex(s, e), fg=RED).pack(side="right")
        return build

    def _ag_cards(self, status):
        def build(inner):
            if not status:
                tk.Label(inner, text=L.t("Loading…", "Загрузка…"), fg=FG_DIM, bg=BG).pack(anchor="w")
                return
            if status.get("ok") is False:
                tk.Label(inner, text=status.get("error") or L.t("Failed to load accounts", "Не удалось загрузить аккаунты"),
                         fg=RED, bg=BG, wraplength=320, justify="left").pack(anchor="w")
                return
            targets = status.get("targets") or {}
            ide = (targets.get("ide") or {})
            bits = []
            if ide.get("installed"):
                bits.append("IDE")
            if (targets.get("cli") or {}).get("available"):
                bits.append("CLI")
            if bits:
                tk.Label(inner, text=L.t("Detected: ", "Найдено: ") + ", ".join(bits),
                         fg=FG_DIM, bg=BG, font=("Segoe UI", 8)).pack(anchor="w", pady=(0, 4))
            profiles = status.get("profiles") or []
            if not profiles:
                tk.Label(inner, text=L.t("No saved accounts yet", "Сохранённых аккаунтов пока нет"),
                         fg=FG_DIM, bg=BG).pack(anchor="w")
                tk.Label(inner, text=L.t("Add an account using +", "Добавьте аккаунт через +"),
                         fg=FG_DIM, bg=BG, font=("Segoe UI", 8)).pack(anchor="w")
                return
            active = status.get("active") or {}
            for profile in profiles:
                pid = profile.get("id")
                email = profile.get("email") or pid
                is_active = pid in active.values()
                card = self._card(inner, is_active)
                shown = email if email in self.revealed else mask_email(email)
                top = tk.Frame(card, bg=card["bg"])
                top.pack(fill="x")
                mail = tk.Label(top, text=shown, fg=FG, bg=card["bg"], font=("Segoe UI", 10, "bold"), cursor="hand2")
                mail.pack(side="left")
                mail.bind("<Button-1>", lambda _e, e=email: self._toggle_email(e))
                plan = profile.get("plan") or ""
                if plan:
                    tk.Label(top, text=str(plan).upper(), fg=BLUE, bg=card["bg"], font=("Segoe UI", 8)).pack(side="right")
                tags = []
                if active.get("cli") == pid:
                    tags.append(L.t("Active (CLI)", "Активен (CLI)"))
                if active.get("ide") == pid:
                    tags.append(L.t("Active (IDE)", "Активен (IDE)"))
                if tags:
                    tk.Label(card, text=" · ".join(tags), fg=GREEN, bg=card["bg"], font=("Segoe UI", 8)).pack(anchor="w")
                quota = profile.get("quota") or {}
                for key, label in (("gemini", "Gemini"), ("third_party", "Claude / GPT")):
                    group = quota.get(key) or {}
                    if group.get("ok") is not True:
                        continue
                    primary = group.get("primary") or {}
                    secondary = group.get("secondary") or {}
                    tk.Label(card, text=label, fg=FG_DIM, bg=card["bg"], font=("Segoe UI", 8)).pack(anchor="w")
                    UsageBar(card, L.t("5h", "5 ч"), remaining(primary.get("used_percent")),
                             primary.get("reset_at"), group.get("allowed") is not False).pack(fill="x")
                    UsageBar(card, L.t("Week", "Неделя"), remaining(secondary.get("used_percent")),
                             secondary.get("reset_at"), group.get("allowed") is not False).pack(fill="x")
                actions = tk.Frame(card, bg=card["bg"])
                actions.pack(fill="x", pady=(4, 0))
                self._btn(actions, L.t("Switch", "Переключить"),
                          lambda i=pid: self.app.switch_ag(i, "all"), bg=BLUE).pack(side="left")
                self._btn(actions, "CLI", lambda i=pid: self.app.switch_ag(i, "cli")).pack(side="left", padx=4)
                self._btn(actions, "IDE", lambda i=pid: self.app.switch_ag(i, "ide")).pack(side="left")
                self._btn(actions, L.t("Remove", "Удалить"),
                          lambda i=pid: self.app.remove_ag(i), fg=RED).pack(side="right")
        return build

    def _toggle_email(self, email):
        if email in self.revealed:
            self.revealed.discard(email)
        else:
            self.revealed.add(email)
        self.app.redraw()

    def _settings(self):
        win = tk.Toplevel(self)
        win.title(L.t("App settings", "Настройки приложения"))
        win.configure(bg=BG)
        win.resizable(False, False)
        tk.Label(win, text=L.t("Language", "Язык"), fg=FG, bg=BG, font=("Segoe UI", 10, "bold")).pack(anchor="w", padx=16, pady=(14, 6))
        var = tk.StringVar(value=L.mode)

        def apply_lang():
            L.set_mode(var.get())
            cfg = load_ui_config()
            cfg["language"] = L.mode
            save_ui_config(cfg)
            self.app.rebuild()

        for value, label in (("system", L.t("System", "Системный")), ("en", "English"), ("ru", "Русский")):
            tk.Radiobutton(
                win, text=label, value=value, variable=var, command=apply_lang,
                fg=FG, bg=BG, selectcolor=BG_CARD, activebackground=BG, activeforeground=FG,
                font=("Segoe UI", 9),
            ).pack(anchor="w", padx=20)

        launch = tk.BooleanVar(value=launch_at_login_enabled())

        def apply_launch():
            set_launch_at_login(launch.get())

        tk.Checkbutton(
            win, text=L.t("Launch at login", "Запускать при входе"), variable=launch,
            command=apply_launch, fg=FG, bg=BG, selectcolor=BG_CARD,
            activebackground=BG, activeforeground=FG, font=("Segoe UI", 9),
        ).pack(anchor="w", padx=16, pady=12)
        self._btn(win, L.t("Close", "Закрыть"), win.destroy).pack(pady=(0, 14))

    def place_near_tray(self):
        self.update_idletasks()
        width, height = 760, 560
        sw = self.winfo_screenwidth()
        sh = self.winfo_screenheight()
        x = max(8, sw - width - 24)
        y = max(8, sh - height - 72)
        self.geometry("%dx%d+%d+%d" % (width, height, x, y))


class App:
    def __init__(self):
        cfg = load_ui_config()
        L.set_mode(cfg.get("language") or "system")
        self.root = tk.Tk()
        self.root.withdraw()
        self.root.title("KeySwitcher")
        self.events = queue.Queue()
        self.tray = TrayIcon(self.events)
        self.dashboard = None
        self.codex = None
        self.antigravity = None
        self._busy = False
        try:
            self.tray.create()
            _log("tray icon created")
        except Exception as exc:
            _log("tray icon failed: %s" % exc)
            self.tray = None
        self.root.after(50, self._safe(self._poll_events, "poll"))
        self.root.after(200, self._safe(self.refresh, "refresh"))
        self.root.after(400, self._safe(self.toggle, "toggle"))
        self.root.protocol("WM_DELETE_WINDOW", self.hide)

    def _safe(self, fn, name):
        def wrapped():
            try:
                fn()
            except Exception as exc:
                _log("%s failed: %s" % (name, exc))
        return wrapped

    def _poll_events(self):
        if self.tray:
            try:
                self.tray.pump_once()
            except Exception as exc:
                _log("pump_once: %s" % exc)
        while True:
            try:
                kind, _payload = self.events.get_nowait()
            except queue.Empty:
                break
            if kind == "toggle":
                self.toggle()
            elif kind == "menu":
                self._context_menu()
        self.root.after(80, self._safe(self._poll_events, "poll"))

    def _context_menu(self):
        menu = tk.Menu(self.root, tearoff=0, bg=BG_CARD, fg=FG, activebackground=BLUE)
        menu.add_command(label=L.t("Open", "Открыть"), command=self.show)
        menu.add_command(label=L.t("Refresh", "Обновить"), command=self.refresh)
        menu.add_separator()
        menu.add_command(label=L.t("Quit", "Выйти"), command=self.quit_app)
        try:
            menu.tk_popup(self.root.winfo_pointerx(), self.root.winfo_pointery())
        finally:
            menu.grab_release()

    def ensure_dashboard(self):
        if self.dashboard is None or not self.dashboard.winfo_exists():
            _log("creating dashboard")
            self.dashboard = Dashboard(self)
            self.dashboard.place_near_tray()
            _log("dashboard created")
        return self.dashboard

    def show(self):
        dash = self.ensure_dashboard()
        dash.deiconify()
        dash.lift()
        dash.place_near_tray()
        self.redraw()

    def hide(self):
        if self.dashboard and self.dashboard.winfo_exists():
            self.dashboard.withdraw()

    def toggle(self):
        if self.dashboard and self.dashboard.winfo_exists() and self.dashboard.state() == "normal":
            self.hide()
        else:
            self.show()

    def rebuild(self):
        if self.dashboard and self.dashboard.winfo_exists():
            self.dashboard.destroy()
        self.dashboard = None
        self.show()

    def redraw(self):
        if self.dashboard and self.dashboard.winfo_exists():
            self.dashboard.render(self.codex, self.antigravity)

    def _set_status(self, text, error=False):
        if self.dashboard and self.dashboard.winfo_exists():
            self.dashboard.set_status(text, error=error)
        parts = []
        accounts = (self.codex or {}).get("accounts") or []
        active = next((a for a in accounts if a.get("active")), None)
        if active:
            usage = active.get("usage") or {}
            window = usage.get("primary") or usage.get("secondary") or {}
            rem = remaining(window.get("used_percent"))
            if rem is not None:
                parts.append("Codex: %d%%" % rem)

        cfg = (self.codex or {}).get("config") or {}
        target_cfg = cfg.get("antigravity_tray_target", "both")
        models_cfg = cfg.get("antigravity_tray_models", "both")
        show_gemini = models_cfg != "claude_gpt"
        show_claude = models_cfg != "gemini"

        ag_active = (self.antigravity or {}).get("active") or {}
        ag_profiles = (self.antigravity or {}).get("profiles") or []
        cli_id = ag_active.get("cli")
        ide_id = ag_active.get("ide")
        cli_profile = next((p for p in ag_profiles if p.get("id") == cli_id), None)
        ide_profile = next((p for p in ag_profiles if p.get("id") == ide_id), None)

        sections = []
        if target_cfg == "cli":
            if cli_profile or ag_profiles:
                sections.append(("", cli_profile or ag_profiles[0]))
        elif target_cfg == "ide":
            if ide_profile or ag_profiles:
                sections.append(("", ide_profile or ag_profiles[0]))
        else:
            if cli_profile and ide_profile and cli_profile.get("id") != ide_profile.get("id"):
                sections.append(("CLI: ", cli_profile))
                sections.append(("IDE: ", ide_profile))
            elif cli_profile or ide_profile or ag_profiles:
                sections.append(("", cli_profile or ide_profile or ag_profiles[0]))

        for prefix, profile in sections:
            quota = profile.get("quota") or {}
            subparts = []
            if show_gemini:
                g_usage = quota.get("gemini") or {}
                if g_usage.get("ok") and not g_usage.get("stale"):
                    w = g_usage.get("primary") or g_usage.get("secondary") or {}
                    g_rem = remaining(w.get("used_percent"))
                    if g_rem is not None:
                        subparts.append("Gemini: %d%%" % g_rem)
            if show_claude:
                t_usage = quota.get("third_party") or {}
                if t_usage.get("ok") and not t_usage.get("stale"):
                    w = t_usage.get("primary") or t_usage.get("secondary") or {}
                    t_rem = remaining(w.get("used_percent"))
                    if t_rem is not None:
                        subparts.append("Claude: %d%%" % t_rem)
            if subparts:
                label = prefix + ", ".join(subparts) if prefix else ", ".join(subparts)
                parts.append(label)

        tip = "KeySwitcher"
        if parts:
            tip = "KeySwitcher  " + " | ".join(parts)
        if text:
            tip = "%s  ·  %s" % (tip, text)
        if self.tray:
            self.tray.set_tip(tip)

    def _work(self, fn, ok_message=None):
        if self._busy:
            return
        self._busy = True
        self._set_status(L.t("Working…", "Работаю…"))

        def runner():
            error = None
            try:
                fn()
            except Exception as exc:
                error = str(exc)
            def done():
                self._busy = False
                try:
                    if error:
                        _log("job error: %s" % error)
                        self._set_status(error, error=True)
                    else:
                        _log("job ok: %s" % (ok_message or "refresh"))
                        self._set_status(ok_message or "")
                        self.redraw()
                except Exception as exc:
                    _log("job done failed: %s" % exc)
            self.root.after(0, done)
        threading.Thread(target=runner, daemon=True).start()

    def refresh(self):
        def job():
            self.codex = run_engine(["status"])
            self.antigravity = run_engine(["antigravity", "status"])
        self._work(job)

    def switch_codex(self, slot):
        def job():
            result = run_engine(["switch", str(slot)])
            if not result.get("ok"):
                raise RuntimeError(result.get("error") or L.t("Failed to switch account", "Не удалось переключить аккаунт"))
            self.codex = run_engine(["status"])
        self._work(job, L.t("Account switched", "Аккаунт переключён"))

    def add_codex(self):
        def job():
            result = run_engine(["add"])
            if not result.get("ok"):
                raise RuntimeError(result.get("error") or L.t("Failed to add account", "Не удалось добавить аккаунт"))
            self.codex = run_engine(["status"])
        self._work(job, L.t("New account saved", "Новый аккаунт сохранён"))

    def relogin_codex(self, slot):
        def job():
            result = run_engine(["relogin", str(slot)])
            if not result.get("ok"):
                raise RuntimeError(result.get("error") or L.t("Failed to update authorization", "Не удалось обновить авторизацию"))
            self.codex = run_engine(["status"])
        self._work(job, L.t("Authorization updated", "Авторизация обновлена"))

    def delete_codex(self, slot, email):
        if not self._confirm(L.t("Delete account?", "Удалить аккаунт?"),
                             L.t("Remove account %s from KeySwitcher?" % email,
                                 "Удалить аккаунт %s из KeySwitcher?" % email)):
            return
        def job():
            result = run_engine(["delete", str(slot)])
            if not result.get("ok"):
                raise RuntimeError(result.get("error") or L.t("Failed to delete account", "Не удалось удалить аккаунт"))
            self.codex = run_engine(["status"])
        self._work(job, L.t("Account removed from switcher", "Аккаунт удалён из свитчера"))

    def add_antigravity(self):
        def job():
            started = run_engine(["antigravity", "begin-login", "ide"])
            if not started.get("ok"):
                raise RuntimeError(started.get("error") or L.t("Failed to add account", "Не удалось добавить аккаунт"))
            deadline = time.time() + 300
            while time.time() < deadline:
                finished = run_engine(["antigravity", "finish-login", "ide"])
                if finished.get("ok") and not finished.get("pending"):
                    self.antigravity = run_engine(["antigravity", "status"])
                    return
                if finished.get("ok") is False:
                    raise RuntimeError(finished.get("error") or L.t("Failed to complete login", "Не удалось завершить вход"))
                time.sleep(2)
            raise RuntimeError(L.t("Login timed out — please try again", "Вход не завершён вовремя — попробуйте ещё раз"))
        self._work(job, L.t("New account saved", "Новый аккаунт сохранён"))

    def switch_ag(self, profile_id, target):
        def job():
            result = run_engine(["antigravity", "switch", profile_id, target])
            if not result.get("ok"):
                raise RuntimeError(result.get("error") or L.t("Failed to switch account", "Не удалось переключить аккаунт"))
            self.antigravity = run_engine(["antigravity", "status"])
        self._work(job, L.t("Account switched", "Аккаунт переключён"))

    def remove_ag(self, profile_id):
        if not self._confirm(L.t("Remove account from switcher?", "Удалить аккаунт из свитчера?"),
                             L.t("Active Antigravity session will stay active. Only saved snapshot in KeySwitcher will be removed.",
                                 "Текущая сессия в Antigravity останется активной. Удалится только сохранённая копия из KeySwitcher.")):
            return
        def job():
            result = run_engine(["antigravity", "remove", profile_id, "all"])
            if not result.get("ok"):
                raise RuntimeError(result.get("error") or L.t("Failed to delete account", "Не удалось удалить аккаунт"))
            self.antigravity = run_engine(["antigravity", "status"])
        self._work(job, L.t("Account removed from switcher", "Аккаунт удалён из свитчера"))

    def _confirm(self, title, message):
        win = tk.Toplevel(self.root)
        win.title(title)
        win.configure(bg=BG)
        win.resizable(False, False)
        tk.Label(win, text=message, fg=FG, bg=BG, wraplength=360, justify="left",
                 font=("Segoe UI", 9)).pack(padx=16, pady=16)
        result = {"ok": False}
        row = tk.Frame(win, bg=BG)
        row.pack(pady=(0, 12))

        def yes():
            result["ok"] = True
            win.destroy()

        tk.Button(row, text=L.t("Delete", "Удалить"), command=yes, bg=RED, fg="white",
                  relief="flat", padx=12, pady=4).pack(side="left", padx=6)
        tk.Button(row, text=L.t("Cancel", "Отмена"), command=win.destroy, bg=BG_CARD, fg=FG,
                  relief="flat", padx=12, pady=4).pack(side="left")
        win.transient(self.root)
        win.grab_set()
        self.root.wait_window(win)
        return result["ok"]

    def quit_app(self):
        _log("quit requested")
        if self.tray:
            self.tray.destroy()
        self.root.destroy()

    def run(self):
        self.root.mainloop()


def _log_path():
    path = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "KeySwitcher" / "Logs"
    path.mkdir(parents=True, exist_ok=True)
    return path / "tray.log"


def _log(message):
    line = "%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), message)
    with contextlib.suppress(Exception):
        with open(_log_path(), "a", encoding="utf-8") as handle:
            handle.write(line)


def main():
    if sys.platform != "win32":
        print("This frontend is the Windows port. Use the Swift app on macOS.")
        return 2
    if resolve_engine() is None:
        print("keyswitcher.py not found")
        return 1
    _log("start executable=%s engine=%s" % (sys.executable, resolve_engine()))
    try:
        App().run()
        _log("mainloop exited")
        return 0
    except Exception:
        import traceback
        text = traceback.format_exc()
        _log(text)
        print(text)
        return 1


if __name__ == "__main__":
    sys.exit(main())
