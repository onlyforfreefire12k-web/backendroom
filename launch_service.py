"""
launch_service.py
-----------------
Short-lived opaque LAUNCH CODES for Telegram Mini App room launches.

Why they exist
--------------
The Mini App direct link can only carry a tiny payload:

    https://t.me/<bot>/<mini_app_short_name>?startapp=<PAYLOAD>

(512 chars max, alphabet A-Za-z0-9_-). The signed identity/session tokens
used elsewhere in this backend are far too long for that — and must never be
truncated. Instead, this service issues a cryptographically random SHORT code
and stores the sensitive context server-side (Firebase RTDB):

    launchCodes/<code>/
        roomId          Telegram group chat ID (the room)
        createdBy       user id that ran /room
        createdByName   display name at issue time
        createdAt       ms epoch
        expiresAt       ms epoch (TTL from LAUNCH_CODE_TTL_SECONDS)

    roomActiveCode/<roomId> = <code>     (reverse index for rotation)

Security model
--------------
* Codes are token_urlsafe(9) -> ~72-bit unguessable, URL/startapp-safe.
* Short TTL (default 30 min); expired codes are rejected and deleted.
* A new /room ROTATES the room's code — the previous code for that room is
  deleted server-side (safely invalidated), so stale links die quickly.
* The code selects the ROOM only. It never carries user identity: the
  Telegram user's verified identity comes from the signature-checked
  Mini App initData at exchange time, and group membership is re-checked
  via getChatMember. Replaying/forging a code therefore cannot impersonate
  anyone and cannot open rooms of other groups.
* Codes live outside rooms/… (top-level launchCodes/) — the existing room
  architecture and Firebase paths used by the 3D frontend are unchanged.
"""

from __future__ import annotations

import logging
import secrets
import time

import config
import firebase_service

log = logging.getLogger("backend.launch")

_CODE_BYTES = 9  # 9 bytes -> 12 base64url chars, startapp-safe alphabet


class LaunchCodeError(Exception):
    """Base class for launch-code failures."""


class LaunchCodeInvalidError(LaunchCodeError):
    """Unknown (or rotated-out) launch code."""


class LaunchCodeExpiredError(LaunchCodeError):
    """Launch code existed but its TTL has passed."""


def _now_ms() -> int:
    return int(time.time() * 1000)


def create_launch_code(room_id, created_by, created_by_name: str) -> str:
    """
    Create a fresh launch code for a room and ROTATE the room's code:
    the previously active code for this room is invalidated server-side.
    """
    room_id_str = str(room_id)
    code = secrets.token_urlsafe(_CODE_BYTES)  # 12 chars, [A-Za-z0-9_-]
    now = _now_ms()
    record = {
        "roomId": room_id_str,
        "createdBy": int(created_by),
        "createdByName": created_by_name,
        "createdAt": now,
        "expiresAt": now + config.LAUNCH_CODE_TTL_SECONDS * 1000,
    }

    # Safely invalidate the room's previous code (rotation).
    previous_code = firebase_service.get_ref(
        f"roomActiveCode/{room_id_str}"
    ).get()
    if previous_code and previous_code != code:
        firebase_service.get_ref(f"launchCodes/{previous_code}").delete()

    firebase_service.get_ref(f"launchCodes/{code}").set(record)
    firebase_service.get_ref(f"roomActiveCode/{room_id_str}").set(code)

    log.info(
        "[ROOM] launch code issued room=%s by user=%s ttl=%ss",
        room_id_str,
        int(created_by),
        config.LAUNCH_CODE_TTL_SECONDS,
    )
    return code


def get_or_create_launch_code(room_id, created_by, created_by_name: str) -> str:
    """
    Return the room's CURRENT active launch code when it is still comfortably
    valid; otherwise mint (and rotate to) a fresh one.

    Used for JOIN ROOM buttons on music cards: reusing the active code keeps
    buttons on slightly older cards working for their full TTL instead of
    being invalidated by every unrelated /play.
    """
    room_id_str = str(room_id)
    existing_code = firebase_service.get_ref(
        f"roomActiveCode/{room_id_str}"
    ).get()
    if existing_code:
        record = firebase_service.get_ref(f"launchCodes/{existing_code}").get()
        expires_at = isinstance(record, dict) and record.get("expiresAt")
        # reuse only while more than the buffer remains
        if isinstance(expires_at, (int, float)) and expires_at > _now_ms() + 120_000:
            return existing_code
    return create_launch_code(room_id, created_by, created_by_name)


def resolve_launch_code(code: str) -> dict:
    """
    Resolve a launch code to its stored record (including roomId).

    Raises LaunchCodeInvalidError / LaunchCodeExpiredError on failure.
    """
    code = (code or "").strip()
    if not code:
        raise LaunchCodeInvalidError("empty launch code")

    record = firebase_service.get_ref(f"launchCodes/{code}").get()
    if not isinstance(record, dict) or not record.get("roomId"):
        raise LaunchCodeInvalidError("unknown launch code")

    expires_at = record.get("expiresAt")
    if not isinstance(expires_at, (int, float)):
        raise LaunchCodeInvalidError("launch code has no expiry")

    if expires_at < _now_ms():
        # Opportunistically invalidate the stale code server-side.
        firebase_service.get_ref(f"launchCodes/{code}").delete()
        raise LaunchCodeExpiredError("launch code expired")

    return record
