#!/usr/bin/env python3
import base64
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import antigravity_quota

ENGINE = Path(__file__).with_name("antigravity.py")


def protobuf_varint(value):
    result = bytearray()
    while value >= 0x80:
        result.append((value & 0x7f) | 0x80)
        value >>= 7
    result.append(value)
    return bytes(result)


def protobuf_bytes(field, value):
    return protobuf_varint((field << 3) | 2) + protobuf_varint(len(value)) + value


def oauth_state(access_token, refresh_token):
    oauth = b"".join((
        protobuf_bytes(1, access_token.encode()),
        protobuf_bytes(2, b"Bearer"),
        protobuf_bytes(3, refresh_token.encode()),
    ))
    row = protobuf_bytes(1, base64.b64encode(oauth))
    entry = protobuf_bytes(1, b"oauthTokenInfoSentinelKey") + protobuf_bytes(2, row)
    return base64.b64encode(protobuf_bytes(1, entry)).decode()


def fake_jwt(email, marker):
    def encode(value):
        return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")
    return "%s.%s.%s" % (encode({"alg": "none"}), encode({"email": email}), marker)


def make_db(path, email, token_marker, include_auth_status=True, auth_status_email=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE ItemTable (key TEXT PRIMARY KEY, value BLOB)")
    nested = base64.b64encode(("profile=" + email).encode()).decode()
    user_status = base64.b64encode(b"\x00" + b"0" + nested.encode()).decode()
    encoded_oauth = oauth_state(fake_jwt(email, token_marker), token_marker + "-refresh")
    rows = [
        ("antigravityUnifiedStateSync.oauthToken", encoded_oauth),
        ("antigravityUnifiedStateSync.userStatus", user_status),
    ]
    if include_auth_status:
        rows.append((
            "antigravityAuthStatus",
            json.dumps({
                "email": auth_status_email or email,
                "name": (auth_status_email or email).split("@")[0],
            }),
        ))
    connection.executemany("INSERT INTO ItemTable(key, value) VALUES (?, ?)", rows)
    connection.commit()
    connection.close()
    return encoded_oauth


def replace_login(path, email, token_marker):
    connection = sqlite3.connect(path)
    rows = [
        ("antigravityUnifiedStateSync.oauthToken", oauth_state(
            fake_jwt(email, token_marker), token_marker + "-refresh",
        )),
        ("antigravityAuthStatus", json.dumps({"email": email})),
    ]
    connection.executemany("INSERT OR REPLACE INTO ItemTable(key, value) VALUES (?, ?)", rows)
    connection.commit()
    connection.close()


def read_row(path, key):
    connection = sqlite3.connect(path)
    try:
        row = connection.execute("SELECT value FROM ItemTable WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None
    finally:
        connection.close()


def write_fake_helper(path):
    path.write_text("""#!/usr/bin/env python3
import json, os, pathlib, sys
store_path = pathlib.Path(os.environ['FAKE_KEYCHAIN_STORE'])
try:
    store = json.loads(store_path.read_text())
except Exception:
    store = {}
command, service, account = sys.argv[1:4]
key = service + '|' + account
if command == 'get':
    if key not in store:
        raise SystemExit(44)
    sys.stdout.buffer.write(store[key].encode())
elif command == 'set':
    store[key] = sys.stdin.buffer.read().decode()
    store_path.write_text(json.dumps(store))
elif command == 'delete':
    store.pop(key, None)
    store_path.write_text(json.dumps(store))
else:
    raise SystemExit(2)
""")
    path.chmod(0o700)


def run_engine(env, *args):
    result = subprocess.run(
        [sys.executable, str(ENGINE), *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
        check=True,
    )
    assert len(result.stdout.splitlines()) == 1
    return result.stdout, json.loads(result.stdout)


def main():
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        ide_db = root / "ide/state.vscdb"
        helper = root / "fake-keychain"
        cli = root / "agy"
        keychain_store = root / "keychain.json"
        original_ide_oauth = make_db(
            ide_db, "ide@example.com", "ide-a", auth_status_email="stale@example.com",
        )
        write_fake_helper(helper)
        cli.write_text("#!/bin/sh\nexit 0\n")
        cli.chmod(0o700)

        cli_payload = json.dumps({
            "email": "cli@example.com",
            "auth_method": "consumer",
            "token": {
                "access_token": fake_jwt("cli@example.com", "cli-a"),
                "refresh_token": "fake-refresh-token",
                "expiry": "2099-01-01T00:00:00Z",
            },
        })
        keychain_store.write_text(json.dumps({"gemini|antigravity": cli_payload}))

        env = dict(os.environ)
        env.update({
            "HOME": str(root),
            "KEYSWITCHER_ANTIGRAVITY_ROOT": str(root / "state"),
            "KEYSWITCHER_ANTIGRAVITY_IDE_DB": str(ide_db),
            "KEYSWITCHER_ANTIGRAVITY_KEYCHAIN_HELPER": str(helper),
            "KEYSWITCHER_ANTIGRAVITY_CLI": str(cli),
            "KEYSWITCHER_ANTIGRAVITY_SKIP_APP_CONTROL": "1",
            "KEYSWITCHER_ANTIGRAVITY_SKIP_QUOTA": "1",
            "FAKE_KEYCHAIN_STORE": str(keychain_store),
        })

        credentials = antigravity_quota.extract_gui_credentials(original_ide_oauth)
        assert credentials["access_token"].count(".") == 2
        assert credentials["refresh_token"] == "ide-a-refresh"
        legacy_oauth = b"".join((
            protobuf_bytes(1, b"legacy-access"),
            protobuf_bytes(2, b"Bearer"),
            protobuf_bytes(3, b"legacy-refresh"),
        ))
        legacy = base64.b64encode(protobuf_bytes(6, legacy_oauth)).decode()
        credentials = antigravity_quota.extract_legacy_gui_credentials(legacy)
        assert credentials["access_token"] == "legacy-access"

        groups = antigravity_quota.normalize_quota_groups([
            {
                "displayName": "Gemini Models",
                "buckets": [
                    {"bucketId": "five-hour", "remainingFraction": 0.75},
                    {"bucketId": "weekly", "remainingFraction": 0.5,
                     "resetTime": "2099-01-01T00:00:00Z"},
                ],
            },
            {
                "displayName": "Claude and GPT models",
                "buckets": [
                    {"window": "5 hour", "remainingFraction": 1},
                    {"window": "week", "remainingFraction": 0.9},
                ],
            },
        ])
        assert groups["gemini"]["primary"]["used_percent"] == 25
        assert groups["gemini"]["secondary"]["used_percent"] == 50
        assert groups["third_party"]["primary"]["window_minutes"] == 300

        ide_output, ide_capture = run_engine(env, "capture", "ide")
        cli_output, cli_capture = run_engine(env, "capture", "cli")
        assert ide_capture["ok"] and ide_capture["profile"]["email"] == "ide@example.com"
        assert cli_capture["ok"] and cli_capture["profile"]["email"] == "cli@example.com"
        assert "fake-access-token" not in ide_output + cli_output
        assert "fake-refresh-token" not in ide_output + cli_output

        ide_id = ide_capture["profile"]["id"]
        wrong_id = hashlib.sha256(b"stale@example.com").hexdigest()[:16]
        state_path = root / "state/profiles.json"
        saved_state = json.loads(state_path.read_text())
        ide_profile = next(item for item in saved_state["profiles"] if item["id"] == ide_id)
        ide_profile["id"] = wrong_id
        ide_profile["email"] = "stale@example.com"
        saved_state["active"]["ide"] = wrong_id
        saved_state["identity_checked"] = {"ide": int(time.time())}
        state_path.write_text(json.dumps(saved_state))
        keychain = json.loads(keychain_store.read_text())
        correct_key = "com.eugene.keyswitcher.antigravity|%s:ide" % ide_id
        wrong_key = "com.eugene.keyswitcher.antigravity|%s:ide" % wrong_id
        keychain[wrong_key] = keychain.pop(correct_key)
        keychain_store.write_text(json.dumps(keychain))
        _, corrected_status = run_engine(env, "status")
        assert corrected_status["active"]["ide"] == ide_id
        assert all(item["email"] != "stale@example.com" for item in corrected_status["profiles"])

        connection = sqlite3.connect(ide_db)
        connection.execute(
            "UPDATE ItemTable SET value = ? WHERE key = 'antigravityUnifiedStateSync.oauthToken'",
            (oauth_state("changed-access", "changed-refresh"),),
        )
        connection.commit()
        connection.close()

        _, switched = run_engine(env, "switch", ide_id, "ide")
        assert switched["ok"]
        assert read_row(ide_db, "antigravityUnifiedStateSync.oauthToken") == original_ide_oauth
        assert (root / "state/backups/ide.state.vscdb").is_file()

        cli_id = cli_capture["profile"]["id"]
        _, switched_cli = run_engine(env, "switch", cli_id, "cli")
        assert switched_cli["ok"]

        _, status = run_engine(env, "status")
        assert status["ok"] and len(status["profiles"]) == 2
        assert status["active"] == {"ide": ide_id, "cli": cli_id}
        assert status["order"] == {"ide": [ide_id], "cli": [cli_id]}

        cli_before_add = json.loads(keychain_store.read_text())["gemini|antigravity"]
        _, started = run_engine(env, "begin-login", "cli")
        assert started["ok"] and started["pending"]
        keychain = json.loads(keychain_store.read_text())
        assert "gemini|antigravity" in keychain
        cli_result_path = root / "state/cli-login-result.json"
        cli_result_path.write_text(json.dumps({
            "ok": True,
            "email": "new@example.com",
            "credentials": {
                "access_token": fake_jwt("new@example.com", "cli-b"),
                "refresh_token": "cli-b-refresh",
                "token_type": "Bearer",
                "expires_in": 3600,
            },
        }))
        _, finished = run_engine(env, "finish-login", "cli")
        assert finished["ok"] and not finished["pending"]
        assert finished["profile"]["email"] == "new@example.com"
        keychain = json.loads(keychain_store.read_text())
        assert keychain["gemini|antigravity"] == cli_before_add
        assert json.loads(state_path.read_text())["active"]["cli"] == cli_id
        saved_cli_snapshot = json.loads(keychain[
            "com.eugene.keyswitcher.antigravity|%s:cli" % finished["profile"]["id"]
        ])
        saved_cli = json.loads(saved_cli_snapshot["payload"])
        assert saved_cli["email"] == "new@example.com"
        assert saved_cli["token"]["refresh_token"] == "cli-b-refresh"
        assert saved_cli["token"]["expiry"].endswith("Z")

        before_cancel = json.loads(keychain_store.read_text())["gemini|antigravity"]
        _, started = run_engine(env, "begin-login", "cli")
        assert started["ok"] and started["pending"]
        _, cancelled_cli = run_engine(env, "cancel-login", "cli")
        assert cancelled_cli["ok"] and not cancelled_cli["pending"]
        assert json.loads(keychain_store.read_text())["gemini|antigravity"] == before_cancel

        active_before_failed_capture = json.loads(state_path.read_text())["active"]["cli"]
        keychain = json.loads(keychain_store.read_text())
        current_credential = keychain.pop("gemini|antigravity")
        keychain_store.write_text(json.dumps(keychain))
        _, started = run_engine(env, "begin-login", "cli")
        assert started["ok"] and started["pending"]
        _, cancelled_cli = run_engine(env, "cancel-login", "cli")
        assert cancelled_cli["ok"] and not cancelled_cli["pending"]
        assert json.loads(state_path.read_text())["active"]["cli"] == active_before_failed_capture
        keychain = json.loads(keychain_store.read_text())
        keychain["gemini|antigravity"] = current_credential
        keychain_store.write_text(json.dumps(keychain))

        _, started = run_engine(env, "begin-login", "ide")
        assert started["ok"] and started["pending"]
        _, cancelled = run_engine(env, "cancel-login", "ide")
        assert cancelled["ok"] and not cancelled["pending"]
        assert read_row(ide_db, "antigravityUnifiedStateSync.oauthToken") is not None

        ide_before_add = read_row(ide_db, "antigravityUnifiedStateSync.oauthToken")
        _, started = run_engine(env, "begin-login", "ide")
        assert started["ok"] and started["pending"]
        ide_result_path = root / "state/ide-login-result.json"
        ide_result_path.write_text(json.dumps({
            "ok": True,
            "email": "new-ide@example.com",
            "credentials": {
                "access_token": fake_jwt("new-ide@example.com", "ide-b"),
                "refresh_token": "ide-b-refresh",
                "token_type": "Bearer",
            },
        }))
        _, finished_ide = run_engine(env, "finish-login", "ide")
        assert finished_ide["ok"] and not finished_ide["pending"]
        assert finished_ide["profile"]["email"] == "new-ide@example.com"
        assert read_row(ide_db, "antigravityUnifiedStateSync.oauthToken") == ide_before_add
        assert json.loads(state_path.read_text())["active"]["ide"] == ide_id
        keychain = json.loads(keychain_store.read_text())
        saved_ide_snapshot = json.loads(keychain[
            "com.eugene.keyswitcher.antigravity|%s:ide" % finished_ide["profile"]["id"]
        ])
        saved_ide = antigravity_quota.extract_gui_credentials_from_rows(
            saved_ide_snapshot["rows"]
        )
        assert saved_ide["refresh_token"] == "ide-b-refresh"

        new_cli_id = finished["profile"]["id"]
        new_ide_id = finished_ide["profile"]["id"]
        _, reordered = run_engine(env, "reorder", "cli", new_cli_id, cli_id)
        assert reordered["ok"]
        assert reordered["order"]["cli"] == [new_cli_id, cli_id]
        assert reordered["order"]["ide"] == [ide_id, new_ide_id]
        _, invalid_order = run_engine(env, "reorder", "cli", new_cli_id, new_cli_id)
        assert invalid_order["ok"] is False
        _, status = run_engine(env, "status")
        assert status["order"]["cli"] == [new_cli_id, cli_id]

        missing_ide_db = ide_db.with_suffix(".missing")
        ide_db.rename(missing_ide_db)
        _, failed_switch = run_engine(env, "switch", finished_ide["profile"]["id"], "ide")
        assert failed_switch["ok"] is False
        assert json.loads(state_path.read_text())["active"]["ide"] == ide_id
        missing_ide_db.rename(ide_db)

        _, removed = run_engine(env, "remove", cli_id, "cli")
        assert removed["ok"]
        _, status = run_engine(env, "status")
        assert "cli" not in status["active"]
        assert all(
            profile["id"] != cli_id or "cli" not in profile["targets"]
            for profile in status["profiles"]
        )
        assert "gemini|antigravity" in json.loads(keychain_store.read_text())
        _, switched_cli = run_engine(env, "switch", finished["profile"]["id"], "cli")
        assert switched_cli["ok"]

        saved_state = json.loads(state_path.read_text())
        saved_state["profiles"].append({
            "id": "legacy-agent", "email": "wrong@example.com", "targets": ["agent"],
        })
        saved_state["active"]["agent"] = "legacy-agent"
        state_path.write_text(json.dumps(saved_state))
        _, status = run_engine(env, "status")
        assert all("agent" not in profile["targets"] for profile in status["profiles"])
        assert "agent" not in status["active"]

        saved_state = json.loads(state_path.read_text())
        active_cli_id = saved_state["active"]["cli"]
        active_cli = next(
            profile for profile in saved_state["profiles"] if profile["id"] == active_cli_id
        )
        now = int(time.time())

        def quota(gemini_primary, gemini_weekly, third_party_primary=10):
            return {
                "gemini": {
                    "ok": True,
                    "allowed": gemini_primary < 99 and gemini_weekly < 99,
                    "stale": False,
                    "primary": {"used_percent": gemini_primary, "window_minutes": 300},
                    "secondary": {"used_percent": gemini_weekly, "window_minutes": 10080},
                },
                "third_party": {
                    "ok": True,
                    "allowed": third_party_primary < 99,
                    "stale": False,
                    "primary": {"used_percent": third_party_primary, "window_minutes": 300},
                    "secondary": {"used_percent": 10, "window_minutes": 10080},
                },
                "updated_at": now,
            }

        active_cli["quota"] = quota(100, 30)
        spare_id = hashlib.sha256(b"spare@example.com").hexdigest()[:16]
        saved_state["profiles"].append({
            "id": spare_id,
            "email": "spare@example.com",
            "targets": ["cli"],
            "quota": quota(20, 10, third_party_primary=100),
        })
        lower_usage_id = hashlib.sha256(b"lower-usage@example.com").hexdigest()[:16]
        saved_state["profiles"].append({
            "id": lower_usage_id,
            "email": "lower-usage@example.com",
            "targets": ["cli"],
            "quota": quota(1, 1),
        })
        state_path.write_text(json.dumps(saved_state))
        keychain = json.loads(keychain_store.read_text())

        def cli_snapshot(email, marker):
            return json.dumps({
                "kind": "keychain",
                "payload": json.dumps({
                    "email": email,
                    "auth_method": "consumer",
                    "token": {
                        "access_token": fake_jwt(email, marker),
                        "refresh_token": marker + "-refresh",
                        "expiry": "2099-01-01T00:00:00Z",
                    },
                }),
            })

        keychain[
            "com.eugene.keyswitcher.antigravity|%s:cli" % spare_id
        ] = cli_snapshot("spare@example.com", "cli-spare")
        keychain[
            "com.eugene.keyswitcher.antigravity|%s:cli" % lower_usage_id
        ] = cli_snapshot("lower-usage@example.com", "cli-lower-usage")
        keychain_store.write_text(json.dumps(keychain))

        _, reordered = run_engine(
            env, "reorder", "cli", lower_usage_id, active_cli_id, spare_id,
        )
        assert reordered["ok"]

        _, enabled = run_engine(env, "set-auto", "cli", "on")
        assert enabled["autoswitch"] == {"cli": True}
        _, auto = run_engine(env, "auto-check")
        assert auto["ok"] and auto["switched_targets"] == ["cli"], auto
        assert auto["switches"][0]["quota_groups"] == ["gemini"]
        _, status = run_engine(env, "status")
        assert status["active"]["cli"] == spare_id
        assert status["active"]["ide"] == ide_id
        assert status["autoswitch"]["cli"] is True

        _, disabled = run_engine(env, "set-auto", "cli", "off")
        assert disabled["autoswitch"]["cli"] is False
        _, ide_enabled = run_engine(env, "set-auto", "ide", "on")
        assert ide_enabled["autoswitch"] == {"cli": False, "ide": True}
        _, ide_disabled = run_engine(env, "set-auto", "ide", "off")
        assert ide_disabled["autoswitch"] == {"cli": False, "ide": False}

        keychain_probe = root / "keychain-probe"
        probe_helper = root / "probe-helper"
        probe_helper.write_text(
            '#!/bin/sh\n: > "$FAKE_KEYCHAIN_TOUCHED"\nexit 44\n'
        )
        probe_helper.chmod(0o700)
        probe_env = dict(env)
        probe_env.update({
            "KEYSWITCHER_ANTIGRAVITY_KEYCHAIN_HELPER": str(probe_helper),
            "FAKE_KEYCHAIN_TOUCHED": str(keychain_probe),
        })
        _, idle = run_engine(probe_env, "auto-check")
        assert idle["reasons"] == {"cli": "disabled", "ide": "disabled"}
        assert not keychain_probe.exists()

    print("antigravity engine tests passed")


if __name__ == "__main__":
    main()
