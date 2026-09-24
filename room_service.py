"""
room_service.py
---------------
Single source of truth for the Firebase room layout used by the existing
3D frontend, now including a SERVER-SIDE PLAYBACK QUEUE.

rooms/
  {roomId}/                         <- roomId == Telegram group chat ID
    metadata/                       <- LEGACY CONTRACT (unchanged fields)
      currentVideoId      string
      currentTitle        string
      currentThumbnail    string
      playbackMode        "audio" | "video"   (lyrics is mirrored as "video")
      isPlaying           bool
      startedAt           ms epoch — the shared clock for late joiners
      requestedBy         Telegram user id
      requestedByName     Telegram display name
      currentMode         "lyrics" | "video" | "audio"   (additive)
      currentItemId       queue item id of what is playing (additive)
      queueLength         number of items waiting (additive)
    playback/                       <- NEW authoritative queue state
      current/            the one item playing right now (or absent)
        id, videoId, title, thumbnail, mode, requestedBy{telegramUserId,name},
        addedAt, startedAt, isPlaying
      queue/              ordered list of waiting items (same item shape)
      updatedAt
    players/                        <- untouched (written by the frontend)
      {telegramId}/ ...

Design notes
------------
* ONE authoritative queue per room. Never per user, never global: every read
  and write is keyed by the Telegram group chat id, so Group A and Group B
  can never touch each other's queue.
* `rooms/{roomId}/playback` is mutated through Firebase TRANSACTIONS so that
  `/skip` and "the video ended" notifications coming from several browsers
  at the same time can never double-advance the queue.
* After every mutation the legacy `metadata` node is mirrored so the already
  working 3D frontend keeps functioning with zero changes.
* The bot writes ONLY Firebase state. Browsers subscribe to Firebase:
      Telegram -> Python Bot -> Firebase -> all connected frontend clients
"""

from __future__ import annotations

import logging
import time
import uuid

import firebase_service

log = logging.getLogger("backend.rooms")

# "lyrics" -> /play (lyrics video shown on the TV)
# "video"  -> /vplay (normal video on the TV)
# "audio"  -> legacy music-theme mode, still accepted for compatibility
PLAYBACK_MODES = ("audio", "video", "lyrics")

# What the legacy `metadata.playbackMode` field must say for each new mode.
# Lyrics videos are rendered exactly like videos on the TV (no black screen).
_LEGACY_MODE = {"lyrics": "video", "video": "video", "audio": "audio"}


def _room_id_str(room_id) -> str:
    return str(room_id)


def _metadata_ref(room_id):
    return firebase_service.get_ref(f"rooms/{_room_id_str(room_id)}/metadata")


def _playback_ref(room_id):
    return firebase_service.get_ref(f"rooms/{_room_id_str(room_id)}/playback")


def _room_ref(room_id):
    return firebase_service.get_ref(f"rooms/{_room_id_str(room_id)}")


def _now_ms() -> int:
    return int(time.time() * 1000)


# ---------------------------------------------------------------------------
# Queue items
# ---------------------------------------------------------------------------


def build_queue_item(
    *,
    video_id: str,
    title: str,
    thumbnail: str,
    mode: str,
    requested_by,
    requested_by_name: str,
    duration_text: str | None = None,
    duration_sec: int | None = None,
) -> dict:
    """Create a queue item dict (not yet stored)."""
    if mode not in PLAYBACK_MODES:
        raise ValueError(f"invalid playback mode: {mode!r}")
    item = {
        "id": uuid.uuid4().hex[:16],
        "videoId": video_id,
        "title": title,
        "thumbnail": thumbnail or "",
        "url": f"https://www.youtube.com/watch?v={video_id}",
        "mode": mode,
        "requestedBy": {
            "telegramUserId": str(int(requested_by)),
            "name": requested_by_name,
        },
        "addedAt": _now_ms(),
    }
    if duration_text:
        item["durationText"] = duration_text
    if isinstance(duration_sec, int) and duration_sec >= 0:
        item["durationSec"] = duration_sec
    return item


def _start_item(item: dict) -> dict:
    """Mark an item as the currently playing one."""
    started = dict(item)
    started["startedAt"] = _now_ms()
    started["isPlaying"] = True
    return started


# ---------------------------------------------------------------------------
# Legacy metadata mirror (keeps the existing frontend contract alive)
# ---------------------------------------------------------------------------


def _mirror_metadata(room_id, current: dict | None, queue_length: int = 0) -> None:
    if current:
        requested_by = (current.get("requestedBy") or {}).get("telegramUserId") or 0
        try:
            requested_by = int(requested_by)
        except (TypeError, ValueError):
            requested_by = 0
        mode = current.get("mode", "video")
        payload = {
            "currentVideoId": current.get("videoId", ""),
            "currentTitle": current.get("title", ""),
            "currentThumbnail": current.get("thumbnail", ""),
            "playbackMode": _LEGACY_MODE.get(mode, "video"),
            "isPlaying": True,
            "requestedBy": requested_by,
            "requestedByName": (current.get("requestedBy") or {}).get("name", ""),
            "startedAt": current.get("startedAt") or _now_ms(),
            "currentMode": mode,
            "currentItemId": current.get("id", ""),
            "queueLength": queue_length,
        }
    else:
        # Nothing is playing -> the TV must go completely black.
        payload = {
            "currentVideoId": "",
            "currentTitle": "",
            "currentThumbnail": "",
            "playbackMode": "",
            "isPlaying": False,
            "requestedBy": 0,
            "requestedByName": "",
            "startedAt": 0,
            "currentMode": "",
            "currentItemId": "",
            "queueLength": queue_length,
        }
    _metadata_ref(room_id).set(payload)


def _normalize_state(state) -> dict:
    if not isinstance(state, dict):
        return {"current": None, "queue": []}
    queue = state.get("queue")
    if not isinstance(queue, list):
        queue = [] if queue is None else [v for v in queue if isinstance(v, dict)]
    current = state.get("current")
    if not isinstance(current, dict):
        current = None
    return {"current": current, "queue": [q for q in queue if isinstance(q, dict)]}


# ---------------------------------------------------------------------------
# Enqueue (/play and /vplay)
# ---------------------------------------------------------------------------


def enqueue_item(room_id, item: dict) -> dict:
    """
    Add an item to the room's queue.

    * If nothing is playing -> the item starts immediately.
    * If something IS playing -> the item is appended; the currently playing
      song is NEVER interrupted.

    Returns {"started": bool, "position": int, "current": dict,
             "item": dict, "queueLength": int}
    """
    outcome: dict = {}

    def txn(raw_state):
        state = _normalize_state(raw_state)
        current = state["current"]
        queue = state["queue"]

        if current is None:
            started = _start_item(item)
            outcome.update(
                started=True, position=0, current=started, item=started,
                queue_length=len(queue),
            )
            return {"current": started, "queue": queue, "updatedAt": _now_ms()}

        queue = queue + [item]
        outcome.update(
            started=False, position=len(queue), current=current, item=item,
            queue_length=len(queue),
        )
        return {"current": current, "queue": queue, "updatedAt": _now_ms()}

    _playback_ref(room_id).transaction(txn)
    _mirror_metadata(room_id, outcome["current"], outcome["queue_length"])

    log.info(
        "[QUEUE] room=%s %s videoId=%s mode=%s position=%s queueLen=%s",
        _room_id_str(room_id),
        "started" if outcome["started"] else "queued",
        item.get("videoId"),
        item.get("mode"),
        outcome["position"],
        outcome["queue_length"],
    )
    return {
        "started": outcome["started"],
        "position": outcome["position"],
        "current": outcome["current"],
        "item": outcome["item"],
        "queueLength": outcome["queue_length"],
    }


# ---------------------------------------------------------------------------
# Centralized queue advance (/skip AND natural end both use this)
# ---------------------------------------------------------------------------


def advance_queue(room_id, expected_item_id: str | None = None) -> dict:
    """
    Advance the room to the next queue item — the ONE place where playback
    transitions happen.

    * removes the current item
    * promotes the first queued item and gives it a fresh `startedAt`
    * if the queue is empty -> clears playback and sets isPlaying = false
      (the frontend then shows a black TV)

    `expected_item_id` guards against duplicate advances: when several
    browsers report "the video ended" at the same time, only the report that
    matches the item actually playing is honoured. The rest are no-ops.

    Returns {"advanced": bool, "stale": bool, "previous": dict|None,
             "next": dict|None, "stopped": bool, "queueLength": int}
    """
    outcome: dict = {}

    def txn(raw_state):
        state = _normalize_state(raw_state)
        current = state["current"]
        queue = state["queue"]

        # Stale/duplicate notification -> do not advance.
        if expected_item_id is not None and (
            current is None or current.get("id") != expected_item_id
        ):
            outcome.update(
                advanced=False, stale=True, previous=current, next=None,
                stopped=current is None, queue_length=len(queue),
            )
            return {
                "current": current,
                "queue": queue,
                "updatedAt": (raw_state or {}).get("updatedAt") or _now_ms(),
            }

        if queue:
            next_item = _start_item(queue[0])
            remaining = queue[1:]
            outcome.update(
                advanced=True, stale=False, previous=current, next=next_item,
                stopped=False, queue_length=len(remaining),
            )
            return {"current": next_item, "queue": remaining, "updatedAt": _now_ms()}

        outcome.update(
            advanced=True, stale=False, previous=current, next=None,
            stopped=True, queue_length=0,
        )
        return {"current": None, "queue": [], "updatedAt": _now_ms()}

    _playback_ref(room_id).transaction(txn)

    if not outcome.get("stale"):
        _mirror_metadata(room_id, outcome.get("next"), outcome.get("queue_length", 0))

    log.info(
        "[QUEUE] advance room=%s advanced=%s stale=%s stopped=%s nextVideoId=%s",
        _room_id_str(room_id),
        outcome.get("advanced"),
        outcome.get("stale"),
        outcome.get("stopped"),
        (outcome.get("next") or {}).get("videoId"),
    )
    return {
        "advanced": bool(outcome.get("advanced")),
        "stale": bool(outcome.get("stale")),
        "previous": outcome.get("previous"),
        "next": outcome.get("next"),
        "stopped": bool(outcome.get("stopped")),
        "queueLength": outcome.get("queue_length", 0),
    }


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def get_room(room_id):
    """Return the full room node (or None when the room has no state yet)."""
    return _room_ref(room_id).get()


def get_metadata(room_id):
    """Return only rooms/{roomId}/metadata (or None)."""
    return _metadata_ref(room_id).get()


def get_playback(room_id) -> dict:
    """Return {"current": dict|None, "queue": [...]} for this room."""
    return _normalize_state(_playback_ref(room_id).get())


def get_queue_snapshot(room_id) -> dict:
    """Convenience read used by /queue and the HTTP state endpoint."""
    state = get_playback(room_id)
    return {
        "current": state["current"],
        "queue": state["queue"],
        "queueLength": len(state["queue"]),
        "isPlaying": bool(state["current"]),
    }


# ---------------------------------------------------------------------------
# Stop / legacy playback controls
# ---------------------------------------------------------------------------


def stop_playback(room_id) -> dict:
    """
    /stop — stop playback, CLEAR the whole queue and reset playback state.
    The frontend immediately sees isPlaying=false with no video -> black TV.
    """
    previous = get_playback(room_id)
    _playback_ref(room_id).set({"current": None, "queue": [], "updatedAt": _now_ms()})
    _mirror_metadata(room_id, None, 0)
    log.info(
        "[QUEUE] stopped room=%s clearedQueue=%s",
        _room_id_str(room_id),
        len(previous["queue"]),
    )
    return {
        "previous": previous["current"],
        "clearedQueue": len(previous["queue"]),
    }


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
    Force a single item to play right now, replacing current playback and
    clearing the queue.

    Kept for backwards compatibility (and for any direct/administrative use);
    the /play and /vplay commands now go through enqueue_item() so that a new
    request never interrupts the song that is already playing.
    """
    if mode not in PLAYBACK_MODES:
        raise ValueError(f"invalid playback mode: {mode!r}")

    item = _start_item(
        build_queue_item(
            video_id=video_id,
            title=title,
            thumbnail=thumbnail,
            mode=mode,
            requested_by=requested_by,
            requested_by_name=requested_by_name,
        )
    )
    _playback_ref(room_id).set({"current": item, "queue": [], "updatedAt": _now_ms()})
    _mirror_metadata(room_id, item, 0)
    log.info(
        "[PLAY] Firebase updated room=%s mode=%s videoId=%s requestedByName=%r",
        _room_id_str(room_id),
        mode,
        video_id,
        requested_by_name,
    )
    return item


def pause_playback(room_id) -> bool:
    """
    Pause playback and persist the current position so /resume and
    late-joining browsers can continue from the same spot.
    Returns False when nothing is playing.
    """
    state = get_playback(room_id)
    current = state["current"]
    if not current or not current.get("isPlaying"):
        return False

    paused_position_ms = None
    started_at = current.get("startedAt")
    if isinstance(started_at, (int, float)):
        paused_position_ms = max(0, _now_ms() - int(started_at))

    current = dict(current)
    current["isPlaying"] = False
    if paused_position_ms is not None:
        current["pausedPositionMs"] = paused_position_ms
    current["pausedAt"] = _now_ms()

    _playback_ref(room_id).update({"current": current, "updatedAt": _now_ms()})
    update = {"isPlaying": False, "pausedAt": firebase_service.server_timestamp()}
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
    state = get_playback(room_id)
    current = state["current"]
    if not current or current.get("isPlaying") or not current.get("videoId"):
        return False

    paused_position_ms = current.get("pausedPositionMs")
    now_ms = _now_ms()
    if isinstance(paused_position_ms, (int, float)):
        new_started_at = now_ms - int(paused_position_ms)
    else:
        new_started_at = now_ms

    current = dict(current)
    current["isPlaying"] = True
    current["startedAt"] = int(new_started_at)
    current.pop("pausedPositionMs", None)

    _playback_ref(room_id).update({"current": current, "updatedAt": now_ms})
    _metadata_ref(room_id).update(
        {
            "isPlaying": True,
            "startedAt": int(new_started_at),
            "resumedAt": firebase_service.server_timestamp(),
        }
    )
    log.info("[ROOM] playback resumed room=%s", _room_id_str(room_id))
    return True
