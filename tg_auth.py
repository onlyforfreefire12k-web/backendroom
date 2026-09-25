"""
tg_auth.py
----------
Telegram Mini App authentication & membership verification.

Two jobs:

1. Validate the `initData` string that Telegram injects into every Mini App.
   Telegram signs it with the bot's token, so a validated initData is a
   cryptographically VERIFIED Telegram identity — the frontend never has to
   be trusted and nothing user-supplied can be forged.

   Official algorithm (https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app):
     secret_key      = HMAC_SHA256(key="WebAppData", msg=<bot_token>)
     data_check_str  = sorted "key=<value>" pairs (excluding `hash`), joined by "\n"
     expected_hash   = hex(HMAC_SHA256(key=secret_key, msg=data_check_str))
     -> compare with the received `hash` using a constant-time comparison.

2. Verify that the Telegram user is actually a member of the group whose
   room they are entering, using the Bot API getChatMember endpoint
   (server-side only — the bot token never reaches the frontend).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from urllib.parse import parse_qsl

import requests

import config

log = logging.getLogger("backend.tg_auth")

_BOT_API_BASE = "https://api.telegram.org"
_TIMEOUT_SECONDS = 10

_MEMBER_OK_STATUSES = ("creator", "administrator", "member")


class InitDataError(Exception):
    """Base class for initData failures."""


class InitDataInvalidError(InitDataError):
    """Signature mismatch or malformed initData."""


class InitDataExpiredError(InitDataError):
    """Valid signature, but auth_date is older than the allowed max age."""


class MembershipCheckError(Exception):
    """The Bot API could not answer the membership question (network/API)."""


# ---------------------------------------------------------------------------
# initData validation
# ---------------------------------------------------------------------------


def validate_init_data(init_data: str) -> dict:
    """
    Validate raw Telegram Mini App initData.

    Returns the parsed, verified fields:
        {"user": {...}|None, "auth_date": int, "start_param": str|None,
         "chat_instance": str|None, "chat_type": str|None, "fields": dict}

    Raises InitDataInvalidError / InitDataExpiredError on failure.
    """
    if not init_data or not isinstance(init_data, str):
        raise InitDataInvalidError("empty init_data")
    if not config.TELEGRAM_BOT_TOKEN:
        raise InitDataInvalidError("bot token not configured")

    try:
        pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    except ValueError as exc:
        raise InitDataInvalidError("init_data is not valid query encoding") from exc

    received_hash = pairs.pop("hash", None)
    if not received_hash:
        raise InitDataInvalidError("init_data has no hash")

    data_check_string = "\n".join(
        f"{key}={value}" for key, value in sorted(pairs.items())
    )
    secret_key = hmac.new(
        b"WebAppData", config.TELEGRAM_BOT_TOKEN.encode("utf-8"), hashlib.sha256
    ).digest()
    expected_hash = hmac.new(
        secret_key, data_check_string.encode("utf-8"), hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(expected_hash, received_hash):
        raise InitDataInvalidError("init_data hash mismatch")

    try:
        auth_date = int(pairs.get("auth_date") or "0")
    except (TypeError, ValueError) as exc:
        raise InitDataInvalidError("init_data has invalid auth_date") from exc

    if auth_date <= 0:
        raise InitDataInvalidError("init_data missing auth_date")

    age = int(time.time()) - auth_date
    if age < -300:  # tolerate small clock skew only
        raise InitDataInvalidError("init_data auth_date is in the future")
    if age > config.WEBAPP_INITDATA_MAX_AGE_SECONDS:
        raise InitDataExpiredError("init_data is too old")

    user = None
    raw_user = pairs.get("user")
    if raw_user:
        try:
            user = json.loads(raw_user)
        except ValueError as exc:
            raise InitDataInvalidError("init_data user is not valid JSON") from exc

    return {
        "user": user,
        "auth_date": auth_date,
        "start_param": pairs.get("start_param") or None,
        "chat_instance": pairs.get("chat_instance") or None,
        "chat_type": pairs.get("chat_type") or None,
        "fields": pairs,
    }


def display_name_from_user(user: dict) -> str:
    """
    Same priority as the bot's get_display_name, but for the initData user dict:
      1. first_name + last_name
      2. first_name
      3. @username
      4. Telegram ID (final fallback)
    """
    user = user or {}
    first_name = (user.get("first_name") or "").strip()
    last_name = (user.get("last_name") or "").strip()
    username = (user.get("username") or "").strip()

    if first_name and last_name:
        return f"{first_name} {last_name}"
    if first_name:
        return first_name
    if username:
        return f"@{username}"
    return str(user.get("id", "unknown"))


# ---------------------------------------------------------------------------
# Group membership verification (server-side Bot API call)
# ---------------------------------------------------------------------------


def verify_room_membership(room_id, telegram_user_id: int) -> bool:
    """
    Return True when the user is a current member of the group (room),
    False when Telegram says they are not, and raise MembershipCheckError
    when the Bot API itself could not answer.

    Rooms are keyed by Telegram group chat ID, so this is what guarantees
    room isolation: someone holding a launch code for a group they are not
    in cannot enter that room.
    """
    try:
        response = requests.get(
            f"{_BOT_API_BASE}/bot{config.TELEGRAM_BOT_TOKEN}/getChatMember",
            params={"chat_id": str(room_id), "user_id": int(telegram_user_id)},
            timeout=_TIMEOUT_SECONDS,
        )
    except requests.RequestException as exc:
        log.error("[ERROR] getChatMember network error: %s", type(exc).__name__)
        raise MembershipCheckError("network error") from exc

    try:
        data = response.json()
    except ValueError as exc:
        log.error("[ERROR] getChatMember invalid JSON")
        raise MembershipCheckError("invalid response") from exc

    if data.get("ok"):
        result = data.get("result") or {}
        status = result.get("status")
        if status in _MEMBER_OK_STATUSES:
            return True
        if status == "restricted" and result.get("is_member"):
            return True
        return False  # left / kicked / anything else

    # Telegram answered, but not ok.
    error_code = data.get("error_code")
    description = str(data.get("description") or "")
    if error_code == 400 and (
        "user not found" in description.lower()
        or "participant" in description.lower()
    ):
        return False
    log.error(
        "[ERROR] getChatMember api error code=%s (room=%s)", error_code, room_id
    )
    raise MembershipCheckError(f"api error {error_code}")
