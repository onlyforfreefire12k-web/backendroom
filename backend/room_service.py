import base64
import hashlib
import hmac
import json
import logging
import time

from firebase_admin import db

import config
from firebase_service import init_firebase, ref


logger = logging.getLogger(__name__)


class InvalidRoomToken(Exception):
    pass


class ExpiredRoomToken(InvalidRoomToken):
    pass


def _b64encode(data: bytes) -> bytes:
    return base64.urlsafe_b64encode(data).rstrip(b"=")


def _b64decode(data: str) -> bytes:
    if not isinstance(data, str) or not data:
        raise ValueError("invalid base64")

    padding = "=" * (4 - len(data) % 4)
    return base64.urlsafe_b64decode(data + padding)


def get_display_name(user):
    """Priority: first+last, first, @username, Telegram ID."""
    first_name = getattr(user, "first_name", None) or ""
    last_name = getattr(user, "last_name", None) or ""

    if first_name and last_name:
        return f"{first_name} {last_name}".strip()

    if first_name:
        return first_name

    username = getattr(user, "username", None) or ""
    if username:
        return "@" + username

    return str(getattr(user, "id", "Unknown")))


def create_room_token(user, chat_id):
    """Create a signed, short-lived room token."""
    if not config.ROOM_TOKEN_SECRET:
        raise InvalidRoomToken("Room token secret is not configured")

    now = int(time.time())
    payload = {
        "telegramUserId": int(user.id),
        "chatId": int(chat_id),
        "firstName": getattr(user, "first_name", None) or "",
        "lastName": getattr(user, "last_name", None) or "",
        "username": getattr(user, "username", None) or "",
        "displayName": get_display_name(user),
        "issuedAt": now,
        "expiresAt": now + config.ROOM_TOKEN_TTL,
    }

    encoded_payload = _b64encode(
        json.dumps(payload, separators=(",", ":")).encode("utf-8")
    )

    signature = hmac.new(
        config.ROOM_TOKEN_SECRET.encode("utf-8"),
        encoded_payload,
        hashlib.sha256,
    ).digest()

    return encoded_payload.decode("ascii") + "." + _b64encode(signature).decode("ascii")


def validate_room_token(token):
    """Validate a signed room token and return its payload."""
    if not isinstance(token, str) or not token:
        raise InvalidRoomToken("Missing token")

    parts = token.split(".")
    if len(parts) != 2:
        raise InvalidRoomToken("Malformed token")

    msg_b64, sig_b64 = parts

    try:
        signature = _b64decode(sig_b64)
    except Exception as exc:
        raise InvalidRoomToken("Invalid signature encoding") from exc

    expected_signature = hmac.new(
        config.ROOM_TOKEN_SECRET.encode("utf-8"),
        msg_b64.encode("ascii"),
        hashlib.sha256,
    ).digest()

    if not hmac.compare_digest(expected_signature, signature):
        raise InvalidRoomToken("Invalid signature")

    try:
        payload = json.loads(_b64decode(msg_b64).decode("utf-8"))
    except Exception as exc:
        raise InvalidRoomToken("Invalid token payload") from exc

    required_fields = {"telegramUserId", "chatId", "expiresAt"}
    if not required_fields.issubset(payload.keys()):
        raise InvalidRoomToken("Token payload is incomplete")

    if int(payload.get("expiresAt", 0)) < int(time.time()):
        raise ExpiredRoomToken("Token has expired")

    return payload


def set_playback_state(
    room_id,
    video_id,
    title,
    thumbnail,
    playback_mode,
    requested_by,
    requested_by_name,
):
    init_firebase()
    metadata_ref = ref(f"rooms/{room_id}/metadata")
    metadata_ref.update({
        "currentVideoId": str(video_id),
        "currentTitle": title,
        "currentThumbnail": thumbnail,
        "playbackMode": playback_mode,
        "isPlaying": True,
        "startedAt": db.SERVER_TIMESTAMP,
        "requestedBy": int(requested_by),
        "requestedByName": requested_by_name,
    })

    logger.info("[PLAY] Firebase updated for room %s", room_id)


def get_room(room_id):
    init_firebase()
    snapshot = ref(f"rooms/{room_id}").get()
    return snapshot if isinstance(snapshot, dict) else {}


def stop_playback(room_id):
    init_firebase()
    ref(f"rooms/{room_id}/metadata").update({"isPlaying": False})


def pause_playback(room_id):
    init_firebase()
    ref(f"rooms/{room_id}/metadata").update({"isPlaying": False})


def resume_playback(room_id):
    init_firebase()
    ref(f"rooms/{room_id}/metadata").update({"isPlaying": True})
