#!/usr/bin/env python3
"""compat.py self-check: lock + path helpers. Run with python engine/test_compat.py."""

import tempfile
from pathlib import Path

import compat


def test_exclusive_lock_roundtrip():
    lock = Path(tempfile.gettempdir()) / "keyswitcher-compat-lock-test"
    seen = []
    with compat.exclusive_lock(lock):
        seen.append("held")
        assert lock.exists()
    assert seen == ["held"]


def test_paths_are_absolute():
    assert compat.keyswitcher_support_dir().is_absolute()
    assert compat.antigravity_ide_db().is_absolute()
    assert compat.codex_log_dir().is_absolute()
    if compat.IS_WIN:
        assert "Antigravity" in str(compat.antigravity_ide_db())
        assert "Codex" in str(compat.codex_log_dir())
    else:
        assert "Library" in str(compat.antigravity_ide_db())


if __name__ == "__main__":
    test_exclusive_lock_roundtrip()
    test_paths_are_absolute()
    print("ok")
