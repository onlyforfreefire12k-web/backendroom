"""
room_service.py
---------------
Single source of truth for the Firebase room layout used by the existing
3D frontend:

rooms/
  {roomId}/                         <- roomId == Telegram group chat ID
    metadata/
      currentVideoId      string
      currentTitle        string
      currentThumbnail    string
      playbackMode        "audio" | "video"
      isPlaying           bool
      startedAt           Firebase SERVER timestamp (ms) — the shared clock
      requestedBy         Telegram user id
      requestedByName     Telegram display name
    players/
      {telegramId}/                 <- written by the verified frontend
        telegramId, firstName, lastName, username, displayName,
        joinedAt, online

The bot writes ONLY Firebase state. Browsers subscribe to Firebase and update
the 3D room themselves:

    Telegram -> Python Bot -> Firebase -> all connected frontend clients

Nothing here is stored in global Python state — every operation is keyed by
room ID, so unlimited Telegram groups can use the bot concurrently.
"""

from __future__ import annotations

import logging
import time

import firebase_service

log = logging.getLogger("backend.rooms")

PLAYBACK_MODES = ("audio", "video")


def _room_id_str(room_id) -> str:
    return str(room_id)


def _metadata_ref(room_id):
    return firebase_service.get_ref(f"rooms/{_room_id_str(room_id)}/metadata")


def _room_ref(room_id):
    return firebase_service.get_ref(f"rooms/{_room_id_str(room_id)}")


def set_playback_state(
    room_id,
    *,
    video_id: str,
    title: str,
    thumbnail: str,
    mode: str,
    requested_by,
    requested_by_name: str,
) -> dict:
    """
    Write the playback state for a room. Called by /play and /vplay.

    `startedAt` uses the Firebase server timestamp so every browser computes
    `elapsed = now - startedAt` against the same reference and stays in sync.
    """
    if mode not in PLAYBACK_MODES:
        raise ValueError(f"invalid playback mode: {mode!r}")

    payload = {
        "currentVideoId": video_id,
        "currentTitle": title,
        "currentThumbnail": thumbnail,
        "playbackMode": mode,
        "isPlaying": True,
        "requestedBy": int(requested_by),
        "requestedByName": requested_by_name,
        "startedAt": firebase_service.server_timestamp(),
    }
    _metadata_ref(room_id).set(payload)
    log.info(
        "[PLAY] Firebase updated room=%s mode=%s videoId=%s requestedByName=%r",
        _room_id_str(room_id),
        mode,
        video_id,
        requested_by_name,
    )
    return payload


def get_room(room_id):
    """Return the full room node (or None when the room has no state yet)."""
    return _room_ref(room_id).get()


def get_metadata(room_id):
    """Return only rooms/{roomId}/metadata (or None)."""
    return _metadata_ref(room_id).get()


# ---------------------------------------------------------------------------
# Playback control helpers (ready for the optional /stop /pause /resume bot
# commands — the service layer is complete; command wiring is a one-liner).
# ---------------------------------------------------------------------------


def stop_playback(room_id) -> None:
    """Stop playback; the frontend should clear/unload the player."""
    _metadata_ref(room_id).update(
        {"isPlaying": False, "stoppedAt": firebase_service.server_timestamp()}
    )
    log.info("[ROOM] playback stopped room=%s", _room_id_str(room_id))


def pause_playback(room_id) -> bool:
    """
    Pause playback and persist the current position so /resume and
    late-joining browsers can continue from the same spot.
    Returns False when nothing is playing.
    """
    metadata = get_metadata(room_id) or {}
    if not metadata.get("isPlaying"):
        return False

    paused_position_ms = None
    started_at = metadata.get("startedAt")
    if isinstance(started_at, (int, float)):
        paused_position_ms = max(0, int(time.time() * 1000) - int(started_at))

    update = {
        "isPlaying": False,
        "pausedAt": firebase_service.server_timestamp(),
    }
    if paused_position_ms is not None:
        update["pausedPositionMs"] = paused_position_ms

    _metadata_ref(room_id).update(update)
    log.info("[ROOM] playback paused room=%s", _room_id_str(room_id))
    return True


def resume_playback(room_id) -> bool:
    """
    Resume playback from the stored pause position.
    `startedAt` is re-anchored so `now - startedAt == paused position`.
    Returns False when there is nothing to resume.
    """
    metadata = get_metadata(room_id) or {}
    if metadata.get("isPlaying") or not metadata.get("currentVideoId"):
        return False

    paused_position_ms = metadata.get("pausedPositionMs")
    now_ms = int(time.time() * 1000)
    if isinstance(paused_position_ms, (int, float)):
        new_started_at = now_ms - int(paused_position_ms)
    else:
        new_started_at = now_ms

    _metadata_ref(room_id).update(
        {
            "isPlaying": True,
            "startedAt": int(new_started_at),
            "resumedAt": firebase_service.server_timestamp(),
        }
    )
    log.info("[ROOM] playback resumed room=%s", _room_id_str(room_id))
    return True
