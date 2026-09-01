#!/usr/bin/env python3
"""Small engine self-checks; run with /usr/bin/python3 engine/test_keyswitcher.py."""

import keyswitcher


def test_tray_display_config():
    assert keyswitcher.DEFAULT_CONFIG["tray_display"] == "both"
    assert keyswitcher._coerce_config_value("tray_display", "Codex") == "codex"
    try:
        keyswitcher._coerce_config_value("tray_display", "ide")
    except ValueError:
        pass
    else:
        raise AssertionError("invalid tray display should be rejected")

    assert keyswitcher.DEFAULT_CONFIG["antigravity_tray_target"] == "both"
    assert keyswitcher._coerce_config_value("antigravity_tray_target", "cli") == "cli"
    assert keyswitcher._coerce_config_value("antigravity_tray_target", "IDE") == "ide"
    assert keyswitcher._coerce_config_value("antigravity_tray_target", "all") == "both"
    try:
        keyswitcher._coerce_config_value("antigravity_tray_target", "invalid")
    except ValueError:
        pass
    else:
        raise AssertionError("invalid antigravity tray target should be rejected")

    assert keyswitcher.DEFAULT_CONFIG["antigravity_tray_models"] == "both"
    assert keyswitcher._coerce_config_value("antigravity_tray_models", "Gemini") == "gemini"
    assert keyswitcher._coerce_config_value("antigravity_tray_models", "claude_gpt") == "claude_gpt"
    assert keyswitcher._coerce_config_value("antigravity_tray_models", "claude") == "claude_gpt"
    assert keyswitcher._coerce_config_value("antigravity_tray_display", "both") == "both"
    try:
        keyswitcher._coerce_config_value("antigravity_tray_models", "invalid")
    except ValueError:
        pass
    else:
        raise AssertionError("invalid antigravity tray models should be rejected")


def test_confirmed_secondary_reset_replaces_cached_value():
    now = 1_000
    slot = {
        "slot": 1,
        "file": "auth_1.json",
        "readable": True,
        "account_id": "account",
        "tokens": {"access_token": "test", "account_id": "account"},
    }
    cache = {
        "account": {
            "fetched_at": now - 60,
            "usage": {
                "secondary": {
                    "used_percent": 73.0,
                    "reset_at": now + 48 * 3600,
                    "window_minutes": 10080,
                },
            },
        },
    }
    raw = {
        "rate_limit": {
            "allowed": True,
            "secondary_window": {
                "used_percent": 1.0,
                "reset_at": now + 7 * 86400,
                "limit_window_seconds": 7 * 86400,
            },
        },
    }
    responses = [raw, raw]
    original_fetch = keyswitcher.fetch_usage_raw
    keyswitcher.fetch_usage_raw = lambda *_args: responses.pop(0)
    try:
        entry, _update, _failure = keyswitcher._gather_one(
            slot, "account", slot["tokens"], {}, {}, cache, now
        )
    finally:
        keyswitcher.fetch_usage_raw = original_fetch

    assert entry["usage"]["secondary"]["used_percent"] == 1.0
    assert not responses


def test_transient_zero_uses_second_response():
    now = 1_000
    slot = {
        "slot": 1,
        "file": "auth_1.json",
        "readable": True,
        "account_id": "account",
        "tokens": {"access_token": "test", "account_id": "account"},
    }
    cache = {
        "account": {
            "fetched_at": now - 60,
            "usage": {"secondary": {"used_percent": 7.0}},
        },
    }
    responses = [
        {"rate_limit": {"allowed": True, "secondary_window": {"used_percent": 0.0}}},
        {"rate_limit": {"allowed": True, "secondary_window": {"used_percent": 7.0}}},
    ]
    original_fetch = keyswitcher.fetch_usage_raw
    keyswitcher.fetch_usage_raw = lambda *_args: responses.pop(0)
    try:
        entry, _update, _failure = keyswitcher._gather_one(
            slot, "account", slot["tokens"], {}, {}, cache, now
        )
    finally:
        keyswitcher.fetch_usage_raw = original_fetch

    assert entry["usage"]["secondary"]["used_percent"] == 7.0
    assert not responses


def test_weekly_only_primary_window_is_exposed_as_secondary():
    raw = {
        "rate_limit": {
            "allowed": True,
            "primary_window": {
                "used_percent": 36.0,
                "limit_window_seconds": 7 * 86400,
                "reset_at": 2_000,
            },
            "secondary_window": None,
        },
    }

    usage = keyswitcher.normalize_usage(raw, fetched_at=1_000)

    assert usage["primary"] is None
    assert usage["secondary"]["used_percent"] == 36.0
    assert usage["secondary"]["window_minutes"] == 7 * 24 * 60


def test_transient_two_percent_uses_second_response():
    now = 1_000
    slot = {
        "slot": 1,
        "file": "auth_1.json",
        "readable": True,
        "account_id": "account",
        "tokens": {"access_token": "test", "account_id": "account"},
    }
    cache = {
        "account": {
            "fetched_at": now - 60,
            "usage": {
                "primary": {"used_percent": 52.0},
                "secondary": {"used_percent": 27.0},
            },
        },
    }
    responses = [
        {
            "rate_limit": {
                "allowed": True,
                "primary_window": {"used_percent": 2.0},
                "secondary_window": {"used_percent": 2.0},
            },
        },
        {
            "rate_limit": {
                "allowed": True,
                "primary_window": {"used_percent": 52.0},
                "secondary_window": {"used_percent": 27.0},
            },
        },
    ]
    original_fetch = keyswitcher.fetch_usage_raw
    keyswitcher.fetch_usage_raw = lambda *_args: responses.pop(0)
    try:
        entry, _update, _failure = keyswitcher._gather_one(
            slot, "account", slot["tokens"], {}, {}, cache, now
        )
    finally:
        keyswitcher.fetch_usage_raw = original_fetch

    assert entry["usage"]["primary"]["used_percent"] == 52.0
    assert entry["usage"]["secondary"]["used_percent"] == 27.0
    assert not responses


def test_run_rotate_does_not_reacquire_rotator_lock():
    lock_held = False

    @keyswitcher.contextlib.contextmanager
    def fake_lock():
        nonlocal lock_held
        assert not lock_held
        lock_held = True
        try:
            yield
        finally:
            lock_held = False

    class FakeRotator:
        @staticmethod
        def rotate(_slot, restart_app=True):
            with keyswitcher.exclusive_lock():
                pass

    original_import = keyswitcher.import_rotator
    original_read = keyswitcher.read_active_tokens
    original_lock = keyswitcher.exclusive_lock
    keyswitcher.import_rotator = lambda: FakeRotator
    keyswitcher.read_active_tokens = lambda: {"account_id": "account"}
    keyswitcher.exclusive_lock = fake_lock
    try:
        ok, _log = keyswitcher.run_rotate(1, "account", restart_app=False)
        assert ok
    finally:
        keyswitcher.import_rotator = original_import
        keyswitcher.read_active_tokens = original_read
        keyswitcher.exclusive_lock = original_lock


if __name__ == "__main__":
    test_tray_display_config()
    test_confirmed_secondary_reset_replaces_cached_value()
    test_transient_zero_uses_second_response()
    test_transient_two_percent_uses_second_response()
    test_run_rotate_does_not_reacquire_rotator_lock()
    print("ok")
