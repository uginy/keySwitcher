#!/usr/bin/env python3
"""Antigravity token decoding and read-only quota lookup."""

import base64
import json
import threading
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import compat

PROJECT_ENDPOINTS = (
    "https://cloudcode-pa.googleapis.com/v1internal:loadCodeAssist",
    "https://daily-cloudcode-pa.sandbox.googleapis.com/v1internal:loadCodeAssist",
)
QUOTA_ENDPOINTS = (
    "https://daily-cloudcode-pa.sandbox.googleapis.com/v1internal:retrieveUserQuotaSummary",
    "https://daily-cloudcode-pa.googleapis.com/v1internal:retrieveUserQuotaSummary",
    "https://cloudcode-pa.googleapis.com/v1internal:retrieveUserQuotaSummary",
)
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
OAUTH_CLIENT_RE = rb"[0-9]{6,}-[A-Za-z0-9_-]+[.]apps[.]googleusercontent[.]com"
OAUTH_SECRET_RE = rb"GOCSPX-[A-Za-z0-9_-]+"
OFFICIAL_AUTH_BINARIES = compat.official_auth_binaries()
_OAUTH_CLIENTS = None
_OAUTH_CLIENTS_LOCK = threading.Lock()


def _read_varint(data, offset):
    value = 0
    shift = 0
    while offset < len(data) and shift <= 63:
        byte = data[offset]
        offset += 1
        value |= (byte & 0x7f) << shift
        if byte < 0x80:
            return value, offset
        shift += 7
    raise ValueError("Invalid protobuf varint")


def _fields(data):
    offset = 0
    while offset < len(data):
        tag, offset = _read_varint(data, offset)
        field_number, wire_type = tag >> 3, tag & 7
        if field_number == 0:
            raise ValueError("Invalid protobuf field")
        if wire_type == 0:
            value, offset = _read_varint(data, offset)
        elif wire_type == 1:
            value, offset = data[offset:offset + 8], offset + 8
        elif wire_type == 2:
            length, offset = _read_varint(data, offset)
            value, offset = data[offset:offset + length], offset + length
        elif wire_type == 5:
            value, offset = data[offset:offset + 4], offset + 4
        else:
            raise ValueError("Unsupported protobuf wire type")
        if offset > len(data):
            raise ValueError("Truncated protobuf field")
        yield field_number, wire_type, value


def _first_bytes(data, field_number):
    for current, wire_type, value in _fields(data):
        if current == field_number and wire_type == 2:
            return value
    return None


def _decode_base64(value):
    raw = value.encode() if isinstance(value, str) else value
    return base64.b64decode(raw + b"=" * (-len(raw) % 4), validate=False)


def _oauth_payload(payload):
    access = _first_bytes(payload, 1)
    refresh = _first_bytes(payload, 3)
    if access and refresh:
        id_token = _first_bytes(payload, 5)
        return {
            "access_token": access.decode(),
            "refresh_token": refresh.decode(),
            "id_token": id_token.decode() if id_token else None,
        }

    nested = _first_bytes(payload, 1)
    if nested:
        try:
            return _oauth_payload(_decode_base64(nested))
        except Exception:
            pass
    return None


def extract_gui_credentials(encoded_state):
    topic = _decode_base64(encoded_state)
    for field_number, wire_type, entry in _fields(topic):
        if field_number != 1 or wire_type != 2:
            continue
        sentinel = _first_bytes(entry, 1)
        payload_or_row = _first_bytes(entry, 2)
        if sentinel != b"oauthTokenInfoSentinelKey" or not payload_or_row:
            continue

        encoded_payload = _first_bytes(payload_or_row, 1)
        if encoded_payload:
            try:
                parsed = _oauth_payload(_decode_base64(encoded_payload))
                if parsed:
                    return parsed
            except Exception:
                pass

        parsed = _oauth_payload(payload_or_row)
        if parsed:
            return parsed
    raise ValueError("OAuth token is not present in Antigravity state")


def extract_legacy_gui_credentials(encoded_state):
    state = _decode_base64(encoded_state)
    oauth = _first_bytes(state, 6)
    if oauth:
        parsed = _oauth_payload(oauth)
        if parsed:
            return parsed
    raise ValueError("OAuth token is not present in legacy Antigravity state")


def extract_gui_credentials_from_rows(rows):
    unified = rows.get("antigravityUnifiedStateSync.oauthToken")
    if unified:
        try:
            return extract_gui_credentials(unified)
        except Exception:
            pass
    legacy = rows.get("jetskiStateSync.agentManagerInitState")
    if legacy:
        return extract_legacy_gui_credentials(legacy)
    raise ValueError("OAuth token is not present in Antigravity state")


def extract_cli_credentials(raw_payload):
    text = raw_payload.strip()
    if text.startswith("go-keyring-base64:"):
        text = _decode_base64(text.split(":", 1)[1]).decode()
    payload = json.loads(text)
    token = payload.get("token") or {}
    if not token.get("access_token") or not token.get("refresh_token"):
        raise ValueError("Antigravity CLI credential is incomplete")
    return token


def _official_oauth_clients():
    import re

    global _OAUTH_CLIENTS
    if _OAUTH_CLIENTS is not None:
        return _OAUTH_CLIENTS
    with _OAUTH_CLIENTS_LOCK:
        if _OAUTH_CLIENTS is not None:
            return _OAUTH_CLIENTS
        for path in OFFICIAL_AUTH_BINARIES:
            try:
                data = path.read_bytes()
            except OSError:
                continue
            client_ids = list(dict.fromkeys(
                match.decode() for match in re.findall(OAUTH_CLIENT_RE, data)
            ))
            secrets = list(dict.fromkeys(
                match.decode() for match in re.findall(OAUTH_SECRET_RE, data)
            ))
            if client_ids:
                _OAUTH_CLIENTS = [
                    (client_id, secret)
                    for client_id in client_ids
                    for secret in (secrets or [None])
                ]
                return _OAUTH_CLIENTS
        # Don't cache an empty result: binaries may appear later (app install).
        return []


def official_oauth_client():
    clients = _official_oauth_clients()
    if not clients:
        raise RuntimeError("Official Antigravity OAuth client was not found")
    return clients[0]


def refresh_access_token(refresh_token, user_agent):
    for client_id, client_secret in _official_oauth_clients():
        fields = {
            "client_id": client_id,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        }
        if client_secret:
            fields["client_secret"] = client_secret
        request = urllib.request.Request(
            TOKEN_ENDPOINT,
            data=urllib.parse.urlencode(fields).encode(),
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": user_agent,
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                access_token = json.loads(response.read().decode()).get("access_token")
                if access_token:
                    return access_token
        except (OSError, ValueError, urllib.error.HTTPError):
            continue
    raise RuntimeError("Official Antigravity OAuth client was not found")


def _post_json(url, access_token, body, user_agent):
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers={
            "Authorization": "Bearer " + access_token,
            "Content-Type": "application/json",
            "User-Agent": user_agent,
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.loads(response.read().decode())


def extract_plan(payload):
    if not isinstance(payload, dict):
        return None
    paid_tier = payload.get("paidTier") or {}
    current_tier = payload.get("currentTier") or {}

    tier_id = (paid_tier.get("id") or current_tier.get("id") or "").lower()
    tier_name = paid_tier.get("name") or current_tier.get("name") or ""

    if "ultra" in tier_id or "ultra" in tier_name.lower():
        return "Ultra"
    if "pro" in tier_id or "pro" in tier_name.lower():
        return "Pro"
    if "enterprise" in tier_id or "enterprise" in tier_name.lower():
        return "Enterprise"
    if "team" in tier_id or "team" in tier_name.lower():
        return "Team"
    if "standard" in tier_id or "standard" in tier_name.lower():
        return "Standard"
    if "free" in tier_id or "free" in tier_name.lower():
        return "Free"
    if tier_name:
        return tier_name
    return None


def _load_code_assist(access_token, user_agent):
    body = {"metadata": {"ideType": "ANTIGRAVITY"}}
    for endpoint in PROJECT_ENDPOINTS:
        try:
            payload = _post_json(endpoint, access_token, body, user_agent)
            project = payload.get("cloudaicompanionProject")
            plan = extract_plan(payload)
            return project, plan
        except (OSError, ValueError, urllib.error.HTTPError):
            continue
    return None, None


def _reset_at(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return int(parsed.timestamp())
    except ValueError:
        return None


def _remaining_fraction(bucket):
    for key in ("remainingFraction", "remaining_fraction", "remainingFractionValue", "fraction"):
        if bucket.get(key) is not None:
            try:
                return float(bucket[key])
            except (ValueError, TypeError):
                pass
    for key in ("remainingPercent", "remaining_percent", "remainingPercentage"):
        if bucket.get(key) is not None:
            try:
                return float(bucket[key]) / 100.0
            except (ValueError, TypeError):
                pass
    for key in ("usedFraction", "used_fraction"):
        if bucket.get(key) is not None:
            try:
                return 1.0 - float(bucket[key])
            except (ValueError, TypeError):
                pass
    for key in ("usedPercent", "used_percent", "usedPercentage"):
        if bucket.get(key) is not None:
            try:
                return 1.0 - (float(bucket[key]) / 100.0)
            except (ValueError, TypeError):
                pass
    return None


def _window_minutes(bucket):
    for key in ("windowMinutes", "window_minutes", "minutes"):
        if bucket.get(key) is not None:
            try:
                return int(bucket[key])
            except (ValueError, TypeError):
                pass
    for key in ("windowSeconds", "window_seconds", "durationSeconds", "seconds"):
        if bucket.get(key) is not None:
            try:
                return int(bucket[key]) // 60
            except (ValueError, TypeError):
                pass

    window = str(bucket.get("window", "")).lower()
    bucket_id = str(bucket.get("bucketId", "") or bucket.get("id", "")).lower()
    display_name = str(bucket.get("displayName", "") or bucket.get("name", "") or bucket.get("label", "")).lower()

    # Prioritize weekly indicators across window, bucketId, and displayName
    for s in (window, bucket_id, display_name):
        if any(w in s for w in ("week", "weekly", "7d", "7-day", "7 day", "7_day", "604800", "168h", "secondary")):
            return 7 * 24 * 60

    # Prioritize 5-hour indicators across window, bucketId, and displayName
    for s in (window, bucket_id, display_name):
        if any(w in s for w in ("5h", "5 hour", "5-hour", "5_hour", "five hour", "five-hour", "five_hour", "fivehour", "18000", "300m", "short", "primary")):
            return 5 * 60

    return None


def normalize_quota_groups(groups):
    result = {
        "gemini": {"ok": True, "allowed": True},
        "third_party": {"ok": True, "allowed": True},
    }
    found = set()

    for group in groups or []:
        label = " ".join(str(group.get(key, "")) for key in (
            "displayName", "description", "name", "groupId", "id", "group", "title"
        )).lower()
        group_key = "third_party" if any(name in label for name in (
            "claude", "gpt", "3p", "third_party", "thirdparty", "third party", "anthropic", "openai"
        )) else "gemini"
        buckets = group.get("buckets") or []
        for index, bucket in enumerate(buckets):
            minutes = _window_minutes(bucket)
            remaining_val = _remaining_fraction(bucket)
            if remaining_val is None:
                continue
            remaining = max(0.0, min(1.0, float(remaining_val)))

            if not minutes:
                if len(buckets) == 2:
                    minutes = 5 * 60 if index == 0 else 7 * 24 * 60
                elif len(buckets) == 1:
                    minutes = 7 * 24 * 60

            if not minutes:
                continue

            window = {
                "used_percent": round((1.0 - remaining) * 100, 2),
                "window_minutes": minutes,
                "reset_at": _reset_at(bucket.get("resetTime") or bucket.get("reset_time")),
            }
            slot = "primary" if minutes <= 24 * 60 else "secondary"
            previous = result[group_key].get(slot)
            if previous is None or window["used_percent"] > previous["used_percent"]:
                result[group_key][slot] = window
            result[group_key]["allowed"] = result[group_key]["allowed"] and remaining > 0
            found.add(group_key)

    if not found:
        raise ValueError("Antigravity quota summary has no supported buckets")
    for key in result:
        if key not in found:
            result[key] = {"ok": False, "error": "quota_unavailable"}
    return result


def fetch_quota(access_token, refresh_token=None, version="2.3.1"):
    user_agent = compat.antigravity_user_agent(version)
    try:
        return _fetch_quota_with_token(access_token, user_agent)
    except RuntimeError:
        if not refresh_token:
            raise
    refreshed_access_token = refresh_access_token(refresh_token, user_agent)
    return _fetch_quota_with_token(refreshed_access_token, user_agent)


def _fetch_quota_with_token(access_token, user_agent):
    project, plan = _load_code_assist(access_token, user_agent)
    body = {"project": project} if project else {}
    last_error = None
    for endpoint in QUOTA_ENDPOINTS:
        try:
            payload = _post_json(endpoint, access_token, body, user_agent)
            return normalize_quota_groups(payload.get("groups")), plan
        except (OSError, ValueError, urllib.error.HTTPError) as exc:
            last_error = exc
    raise RuntimeError("Antigravity quota request failed") from last_error
