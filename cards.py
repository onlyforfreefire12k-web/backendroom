"""
cards.py
--------
Telegram message presentation layer ("music cards") shared by bot.py
(via python-telegram-bot) and live.py (via the plain Bot API over HTTP).

Everything here is built from REAL data already returned by the YouTube
search (thumbnail / title / duration / video id) and the room queue state.
Nothing is mocked or invented.

Caption style:
    🎵 NOW PLAYING / ADDED TO QUEUE / ⏸ PAUSED / ⏭ SKIPPED / ⏹ QUEUE FINISHED
    <b>bold song title</b>, requester, real duration, actual mode
    (🎵 Lyrics Video for /play, 🎬 Video for /vplay)

Inline keyboards (callback queries only — never fake text commands):
    [ 🚪 Join Room ]                 -> current group's Mini App direct link
    [ ⏸ Pause ] [ ⏭ Skip ] [ 📋 Queue ]   (▶️ Resume instead of Pause when paused)

Security notes
--------------
* Callback data carries the room id AND the queue item id, so a stale card
  or a card from another group cannot control this room's playback.
* The bot token is only used server-side for Bot API HTTP calls; nothing
  secret ever appears in messages, captions, keyboards or logs.
"""

from __future__ import annotations

import html
import logging
import threading

import requests

import config

log = logging.getLogger("backend.cards")

_BOT_API_BASE = "https://api.telegram.org"
_TIMEOUT_SECONDS = 10

_MAX_TITLE = 90  # keep captions compact
_ALERT_MAX = 190  # Telegram alert text limit is 200 chars

_MODE_LABELS = {
    "lyrics": "🎵 Lyrics Video",
    "video": "🎬 Video",
    "audio": "🎵 Music",
}

_bot_username_cache: str | None = None
_bot_username_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Small text helpers
# ---------------------------------------------------------------------------


def esc(text) -> str:
    return html.escape(str(text if text is not None else ""), quote=False)


def _truncate(text: str, limit: int = _MAX_TITLE) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def mode_label(item: dict) -> str:
    return _MODE_LABELS.get((item or {}).get("mode", "video"), "🎬 Video")


def item_requester_name(item: dict) -> str:
    return ((item or {}).get("requestedBy") or {}).get("name", "")


# ---------------------------------------------------------------------------
# Captions (HTML parse mode)
# ---------------------------------------------------------------------------


def _requester_line(item: dict, fallback_label: str = "") -> str | None:
    name = item_requester_name(item) or fallback_label
    return f"👤 Requested by: <b>{esc(name)}</b>" if name else None


def _duration_line(item: dict) -> str | None:
    duration = (item or {}).get("durationText")
    return f"⏱ Duration: {esc(duration)}" if duration else None


def _compose(header: str, item: dict, extra_lines: list[str | None]) -> str:
    lines = [header, "", f"<b>{esc(_truncate(item.get('title', '')))}</b>", ""]
    for line in extra_lines:
        if line:
            lines.append(line)
    return "\n".join(lines)


def now_playing_caption(item: dict, queue_length: int = 0, *, paused: bool = False) -> str:
    header = "⏸ <b>PAUSED</b>" if paused else "🎵 <b>NOW PLAYING</b>"
    queue_line = f"\n\n📋 Next in queue: {queue_length}" if queue_length else None
    return _compose(
        header,
        item,
        [
            _requester_line(item),
            _duration_line(item),
            mode_label(item),
        ],
    ) + (queue_line or "")


def added_to_queue_caption(item: dict, position: int, queue_length: int) -> str:
    return _compose(
        "🎵 <b>ADDED TO QUEUE</b>",
        item,
        [
            _requester_line(item),
            _duration_line(item),
            mode_label(item),
            f"📋 Position in queue: <b>#{position}</b>",
        ],
    )


def paused_caption(item: dict, queue_length: int = 0) -> str:
    return now_playing_caption(item, queue_length, paused=True)


def skipped_caption(item: dict) -> str:
    return f"⏭ <b>SKIPPED</b> — <i>{esc(_truncate(item.get('title', '')))}</i>"


def finished_caption(item: dict) -> str:
    return f"⏹ <b>QUEUE FINISHED</b> — <i>{esc(_truncate(item.get('title', '')))}</i>"


def queue_alert_text(snapshot: dict) -> str:
    """
    Compact queue listing for an answerCallbackQuery alert
    (kept well under Telegram's 200-char alert limit).
    """
    current = snapshot.get("current")
    queue = snapshot.get("queue") or []
    if not current and not queue:
        return "📋 The queue is empty. Use /play or /vplay."

    lines = []
    if current:
        lines.append(f"▶️ {_truncate(current.get('title', ''), 32)}")
    for index, entry in enumerate(queue[:4], start=1):
        lines.append(f"{index}. {_truncate(entry.get('title', ''), 32)}")
    if len(queue) > 4:
        lines.append(f"…and {len(queue) - 4} more")
    text = "\n".join(lines)
    return text if len(text) <= _ALERT_MAX else text[: _ALERT_MAX - 1] + "…"


# ---------------------------------------------------------------------------
# Keyboards (plain dict rows — converted to PTB objects in bot.py)
# ---------------------------------------------------------------------------


def card_keyboard_rows(
    *,
    room_id,
    item_id: str | None = None,
    paused: bool = False,
    active_card: bool = True,
    join_url: str | None = None,
    include_controls: bool = True,
    include_queue: bool = True,
) -> list[list[dict]]:
    """
    `active_card` marks the card that represents the CURRENTLY playing item
    (its callbacks get the ':c' suffix), so the callback handler knows it may
    edit the message when the state changes. Buttons on ADDED-TO-QUEUE cards
    still control the current item but never edit the wrong card.
    """
    tag = ":c" if active_card else ""
    rows: list[list[dict]] = []

    if join_url:
        rows.append([{"text": "🚪 Join Room", "url": join_url}])

    if include_controls and item_id:
        toggle = (
            {"text": "▶️ Resume", "callback_data": f"room_resume:{room_id}:{item_id}{tag}"}
            if paused
            else {"text": "⏸ Pause", "callback_data": f"room_pause:{room_id}:{item_id}{tag}"}
        )
        control_row = [
            toggle,
            {"text": "⏭ Skip", "callback_data": f"room_skip:{room_id}:{item_id}{tag}"},
        ]
        if include_queue:
            control_row.append(
                {"text": "📋 Queue", "callback_data": f"room_queue:{room_id}"}
            )
        rows.append(control_row)
    elif include_queue:
        rows.append(
            [{"text": "📋 Queue", "callback_data": f"room_queue:{room_id}"}]
        )

    return rows


def ended_card_keyboard_rows(*, room_id, join_url: str | None) -> list[list[dict]]:
    rows: list[list[dict]] = []
    if join_url:
        rows.append([{"text": "🚪 Join Room", "url": join_url}])
    rows.append([{"text": "📋 Queue", "callback_data": f"room_queue:{room_id}"}])
    return rows


# ---------------------------------------------------------------------------
# Bot API HTTP helpers (used from live.py; bot.py uses python-telegram-bot)
# ---------------------------------------------------------------------------


def bot_username(force_refresh: bool = False) -> str | None:
    """Resolve (and cache) the bot username via the Bot API getMe call."""
    global _bot_username_cache
    with _bot_username_lock:
        if _bot_username_cache and not force_refresh:
            return _bot_username_cache
        try:
            response = requests.get(
                f"{_BOT_API_BASE}/bot{config.TELEGRAM_BOT_TOKEN}/getMe",
                timeout=_TIMEOUT_SECONDS,
            )
            data = response.json()
            if data.get("ok"):
                _bot_username_cache = data["result"]["username"]
        except Exception as exc:
            log.error("[ERROR] getMe failed type=%s", type(exc).__name__)
        return _bot_username_cache


def _api_post(method: str, payload: dict) -> dict | None:
    """Fire a Bot API call over HTTP. Returns the decoded result or None."""
    try:
        response = requests.post(
            f"{_BOT_API_BASE}/bot{config.TELEGRAM_BOT_TOKEN}/{method}",
            json=payload,
            timeout=_TIMEOUT_SECONDS,
        )
        data = response.json()
    except Exception as exc:
        log.error("[ERROR] Bot API %s failed type=%s", method, type(exc).__name__)
        return None
    if not data.get("ok"):
        log.error(
            "[ERROR] Bot API %s not-ok code=%s", method, data.get("error_code")
        )
        return None
    return data.get("result")


def send_card(
    chat_id,
    photo_url: str | None,
    caption: str,
    keyboard_rows: list[list[dict]] | None,
) -> bool:
    """Send a photo+caption music card; falls back to a text card."""
    markup = (
        {"inline_keyboard": keyboard_rows} if keyboard_rows else None
    )
    if photo_url:
        payload = {
            "chat_id": chat_id,
            "photo": photo_url,
            "caption": caption,
            "parse_mode": "HTML",
        }
        if markup:
            payload["reply_markup"] = markup
        if _api_post("sendPhoto", payload) is not None:
            return True
    payload = {
        "chat_id": chat_id,
        "text": caption,
        "parse_mode": "HTML",
    }
    if markup:
        payload["reply_markup"] = markup
    return _api_post("sendMessage", payload) is not None


def send_text(chat_id, text: str, keyboard_rows: list[list[dict]] | None = None) -> bool:
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    if keyboard_rows:
        payload["reply_markup"] = {"inline_keyboard": keyboard_rows}
    return _api_post("sendMessage", payload) is not None
