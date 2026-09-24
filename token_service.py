"""
token_service.py
----------------
Signed, short-lived "room join" tokens.

Problem being solved
--------------------
The 3D frontend must know WHICH Telegram user opened the room — without
trusting client-supplied query params like ?userId=123 (trivially forged).

Solution
--------
When a user taps JOIN ROOM (deep-link -> /start room_<chatId>), the bot
creates an HMAC-SHA256 signed token containing the user's verified Telegram
identity. Only the backend knows ROOM_TOKEN_SECRET, so tokens cannot be
forged or modified. Tokens expire (default: 1 hour).

The generated join URL contains ONLY the token:

    https://FRONTEND_URL/room/<chatId>?token=SIGNED_TOKEN

The frontend POSTs the token to /api/token/validate and receives the
verified identity for the character name / player record.
"""

from __future__ import annotations

import logging
import time

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

import config

log = logging.getLogger("backend.token")

_SALT = "tg-3d-room-join-v1"

_serializer = URLSafeTimedSerializer(config.ROOM_TOKEN_SECRET or "dev-only-secret")


class TokenError(Exception):
    """Base class for token failures."""


class TokenInvalidError(TokenError):
    """Signature mismatch or malformed payload."""


class TokenExpiredError(TokenError):
    """Signature valid, but the token is older than the allowed TTL."""


def create_room_token(
    *,
    telegram_user_id: int,
    chat_id: int,
    first_name: str | None,
    last_name: str | None,
    username: str | None,
    display_name: str,
) -> str:
    """Create a signed short-lived room token for one Telegram user."""
    now = int(time.time())
    payload = {
        "telegramUserId": int(telegram_user_id),
        "chatId": str(chat_id),
        "firstName": first_name or "",
        "lastName": last_name or "",
        "username": username or "",
        "displayName": display_name,
        "issuedAt": now,
        "expiresAt": now + config.ROOM_TOKEN_TTL_SECONDS,
    }
    return _serializer.dumps(payload, salt=_SALT)


def validate_room_token(token: str) -> dict:
    """
    Validate a room token.

    Returns the verified payload on success.
    Raises TokenInvalidError / TokenExpiredError on failure.
    """
    if not token or not isinstance(token, str):
        raise TokenInvalidError("empty token")

    try:
        payload = _serializer.loads(
            token, salt=_SALT, max_age=config.ROOM_TOKEN_TTL_SECONDS
        )
    except SignatureExpired as exc:
        raise TokenExpiredError("token expired") from exc
    except BadSignature as exc:
        raise TokenInvalidError("bad signature") from exc
    except Exception as exc:  # malformed input, unicode issues, etc.
        raise TokenInvalidError("malformed token") from exc

    now = int(time.time())
    required_keys = (
        "telegramUserId",
        "chatId",
        "displayName",
        "issuedAt",
        "expiresAt",
    )
    if not isinstance(payload, dict) or any(k not in payload for k in required_keys):
        raise TokenInvalidError("payload missing fields")

    try:
        payload["telegramUserId"] = int(payload["telegramUserId"])
        payload["chatId"] = str(payload["chatId"])
        expires_at = int(payload["expiresAt"])
    except (TypeError, ValueError) as exc:
        raise TokenInvalidError("payload field type mismatch") from exc

    if expires_at < now:
        raise TokenExpiredError("token expired")

    return payload
