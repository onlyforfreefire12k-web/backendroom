"""
bot.py
------
Telegram bot for the 3D Music Room.

Commands
--------
/room                 (groups) post the JOIN ROOM Mini App card for this group
/play <song name>     (groups) queue the LYRICS video   -> mode = "lyrics"
/vplay <song name>    (groups) queue the normal video   -> mode = "video"
/skip                 (groups) skip current item, start the next one
/stop                 (groups) stop playback and clear the queue (black TV)
/queue                (groups) show this room's queue

Deep link
---------
/start room_<chatId>  (private) issued when a user taps JOIN ROOM; verifies
                      group membership and returns a SIGNED join URL.

Security notes
--------------
* The JOIN ROOM URL contains only a signed token — never raw identity data.
* Group membership is verified via getChatMember before any token is issued,
  so only real members of a group can receive a join link for that room.
* No global song/room state exists in this process: everything is keyed by
  Telegram chat ID, so any number of groups can use the bot concurrently.
* Secrets are never sent in Telegram messages.
"""

from __future__ import annotations

import asyncio
import logging

from pathlib import Path

from firebase_admin import exceptions as firebase_exceptions
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction, ChatMemberStatus, ChatType, ParseMode
from telegram.error import BadRequest, Forbidden, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

import cards
import config
import launch_service
import room_service
import token_service
import youtube_service

log = logging.getLogger("backend.bot")

_START_PREFIX = "room_"

_GROUP_CHAT_TYPES = (ChatType.GROUP, ChatType.SUPERGROUP)

# Bundled welcome artwork (used for the DM /start panel; falls back to text
# when unavailable so the UI never breaks).
_WELCOME_PHOTO = Path(__file__).resolve().parent / "assets" / "audium-welcome.jpg"

BOT_NAME = "Audium"

# Commands panel — built ONLY from commands that are really implemented
# (see build_application: every entry below has a registered handler).
_COMMAND_PANEL = (
    "📖 <b>Commands</b>\n\n"
    "🎧 /start — Welcome &amp; your personal room\n"
    "🚪 /room — Post this group's JOIN ROOM card\n"
    "🎵 /play &lt;song&gt; — Queue a lyrics-version video\n"
    "🎬 /vplay &lt;song&gt; — Queue a YouTube video\n"
    "⏸ /pause — Pause current playback\n"
    "▶️ /resume — Resume playback\n"
    "⏭ /skip — Skip to the next song\n"
    "⏹ /stop — Stop &amp; clear the queue\n"
    "📋 /queue — Show this room's queue"
)

_WELCOME_CAPTION = (
    "🎧 <b>Audium</b>\n\n"
    "Your synchronized music room for Telegram.\n\n"
    "Create or join a room, play music together and control playback "
    "directly from Telegram."
)

_MEMBER_OK_STATUSES = (
    ChatMemberStatus.OWNER,
    ChatMemberStatus.ADMINISTRATOR,
    ChatMemberStatus.MEMBER,
)


# ---------------------------------------------------------------------------
# Telegram display name (section 4 of the spec)
# ---------------------------------------------------------------------------


def get_display_name(user) -> str:
    """
    Priority:
      1. first_name + last_name
      2. first_name
      3. @username
      4. Telegram ID (final fallback)
    """
    first_name = (getattr(user, "first_name", None) or "").strip()
    last_name = (getattr(user, "last_name", None) or "").strip()
    username = (getattr(user, "username", None) or "").strip()

    if first_name and last_name:
        return f"{first_name} {last_name}"
    if first_name:
        return first_name
    if username:
        return f"@{username}"
    return str(getattr(user, "id", "unknown"))


def _requested_by_label(user, display_name: str) -> str:
    username = (getattr(user, "username", None) or "").strip()
    return f"@{username}" if username else display_name


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _is_group_chat(update: Update) -> bool:
    chat = update.effective_chat
    return bool(chat) and chat.type in _GROUP_CHAT_TYPES


async def _bot_username(context: ContextTypes.DEFAULT_TYPE) -> str:
    cached = context.application.bot_data.get("bot_username")
    if cached:
        return cached
    me = await context.bot.get_me()
    context.application.bot_data["bot_username"] = me.username
    return me.username


def mini_app_launch_link(bot_username: str, launch_code: str) -> str:
    """
    Official Telegram Mini App DIRECT LINK. Tapping it opens the app's
    launch confirmation INSIDE Telegram (never an external browser):

        https://t.me/<bot_username>/<mini_app_short_name>?startapp=<code>

    The payload is ONLY the opaque launch code — never credentials,
    never the bot token, never a signed identity token.
    """
    return (
        f"https://t.me/{bot_username}/{config.MINI_APP_SHORT_NAME}"
        f"?startapp={launch_code}"
    )


def _to_markup(rows: list[list[dict]] | None) -> InlineKeyboardMarkup | None:
    """Convert plain dict rows (built in cards.py) into PTB keyboards."""
    if not rows:
        return None
    keyboard = []
    for row in rows:
        line = []
        for button in row:
            if button.get("url"):
                line.append(InlineKeyboardButton(button["text"], url=button["url"]))
            else:
                line.append(
                    InlineKeyboardButton(
                        button["text"], callback_data=button.get("callback_data")
                    )
                )
        keyboard.append(line)
    return InlineKeyboardMarkup(keyboard)


async def _join_url(context: ContextTypes.DEFAULT_TYPE, room_id, user, display_name: str) -> str | None:
    """Mini App launch URL for the given room (reuses the room's active code)."""
    try:
        bot_username = await _bot_username(context)
        code = await asyncio.to_thread(
            launch_service.get_or_create_launch_code, room_id, user.id, display_name
        )
        return mini_app_launch_link(bot_username, code)
    except Exception as exc:
        log.error("[ERROR] join url build failed type=%s", type(exc).__name__)
        return None


async def _reply_card(
    update_or_query_message,
    *,
    photo_url: str | None,
    caption: str,
    markup: InlineKeyboardMarkup | None,
) -> None:
    """Send a photo+caption card, gracefully falling back to a text card."""
    try:
        if photo_url:
            await update_or_query_message.reply_photo(
                photo=photo_url,
                caption=caption,
                reply_markup=markup,
                parse_mode=ParseMode.HTML,
            )
            return
        raise TelegramError("no photo url")
    except TelegramError:
        await update_or_query_message.reply_text(
            caption, reply_markup=markup, parse_mode=ParseMode.HTML
        )


def _welcome_keyboard(
    open_room_url: str | None,
    add_to_group_url: str,
) -> InlineKeyboardMarkup:
    rows: list[list[dict]] = []
    if open_room_url:
        rows.append([{"text": "🚪 Open Room", "url": open_room_url}])
    rows.append([{"text": "➕ Add to Group", "url": add_to_group_url}])
    rows.append([{"text": "📖 Commands", "callback_data": "ui:commands"}])
    community = []
    if config.CHANNEL_URL:
        community.append({"text": "📢 Channel", "url": config.CHANNEL_URL})
    if config.SUPPORT_URL:
        community.append({"text": "💬 Support", "url": config.SUPPORT_URL})
    if community:
        rows.append(community)
    return _to_markup(rows)


# ---------------------------------------------------------------------------
# /room — post the group's join card
# ---------------------------------------------------------------------------


async def cmd_room(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    user = update.effective_user
    if not update.message:
        return

    if not _is_group_chat(update):
        await update.message.reply_text(
            "The /room command works inside Telegram groups.\n\n"
            "Add me to your group and send /room there."
        )
        return

    try:
        bot_username = await _bot_username(context)
    except TelegramError as exc:
        log.error("[ERROR] could not resolve bot username: %s", type(exc).__name__)
        await update.message.reply_text("Room link is unavailable right now. Try again.")
        return

    room_id = chat.id
    display_name = get_display_name(user)

    # --- Mini App launch code (opaque, short-lived, room-scoped) ---
    try:
        launch_code = await asyncio.to_thread(
            launch_service.create_launch_code,
            room_id,
            user.id,
            display_name,
        )
    except firebase_exceptions.FirebaseError as exc:
        log.error(
            "[ERROR] launch code issue failed chat=%s type=%s",
            room_id,
            type(exc).__name__,
        )
        await update.message.reply_text(
            "❌ Could not open the room right now. Please try again."
        )
        return
    except Exception as exc:
        log.error(
            "[ERROR] unexpected launch code failure chat=%s type=%s",
            room_id,
            type(exc).__name__,
        )
        await update.message.reply_text(
            "❌ Could not open the room right now. Please try again."
        )
        return

    launch_url = mini_app_launch_link(bot_username, launch_code)
    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("🎮 JOIN ROOM", url=launch_url)]]
    )

    await update.message.reply_text(
        "🎵 3D MUSIC ROOM\n\n"
        "Join the group's 3D room — right here inside Telegram.\n\n"
        "Tap JOIN ROOM below. Everyone in this group enters the SAME room, "
        "and your Telegram name is used automatically — no typing, no signup.",
        reply_markup=keyboard,
    )
    log.info(
        "[ROOM] Mini App launch posted chat=%s by user=%s", room_id, user.id
    )


# ---------------------------------------------------------------------------
# /start room_<chatId> — verify membership and issue the signed join URL
# ---------------------------------------------------------------------------


async def _send_welcome(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Modern DM welcome panel for Audium (PART 1)."""
    chat = update.effective_chat
    user = update.effective_user
    display_name = get_display_name(user)

    bot_username = None
    try:
        bot_username = await _bot_username(context)
    except TelegramError as exc:
        log.error("[ERROR] get_me failed in /start: %s", type(exc).__name__)

    # "Open Room" -> the user's PERSONAL room (room id == private chat id)
    # via the existing secure Mini App launch-code flow (never a browser URL).
    open_room_url = None
    if bot_username and chat and chat.type == ChatType.PRIVATE:
        open_room_url = await _join_url(context, chat.id, user, display_name)

    add_to_group_url = (
        f"https://t.me/{bot_username}?startgroup=true" if bot_username else None
    )
    rows: list[list[dict]] = []
    if open_room_url:
        rows.append([{"text": "🚪 Open Room", "url": open_room_url}])
    if add_to_group_url:
        rows.append([{"text": "➕ Add to Group", "url": add_to_group_url}])
    rows.append([{"text": "📖 Commands", "callback_data": "ui:commands"}])
    community = []
    if config.CHANNEL_URL:
        community.append({"text": "📢 Channel", "url": config.CHANNEL_URL})
    if config.SUPPORT_URL:
        community.append({"text": "💬 Support", "url": config.SUPPORT_URL})
    if community:
        rows.append(community)
    markup = _to_markup(rows)

    if _WELCOME_PHOTO.is_file():
        try:
            with open(_WELCOME_PHOTO, "rb") as photo:
                await update.message.reply_photo(
                    photo=photo,
                    caption=_WELCOME_CAPTION,
                    reply_markup=markup,
                    parse_mode=ParseMode.HTML,
                )
            log.info("[ROOM] welcome panel sent user=%s", user.id)
            return
        except TelegramError as exc:
            log.error("[ERROR] welcome photo failed: %s", type(exc).__name__)
    await update.message.reply_text(
        _WELCOME_CAPTION, reply_markup=markup, parse_mode=ParseMode.HTML
    )
    log.info("[ROOM] welcome panel sent (text) user=%s", user.id)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user:
        return

    user = update.effective_user
    chat = update.effective_chat
    display_name = get_display_name(user)
    payload = (context.args[0] if context.args else "").strip()

    if not payload:
        if chat and chat.type == ChatType.PRIVATE:
            await _send_welcome(update, context)
        else:
            await update.message.reply_text(
                "🎧 Audium\n\nSend /room inside your group to open the 3D room."
            )
        return

    if chat and chat.type != ChatType.PRIVATE:
        # Deep links only arrive in private chats; ignore group noise.
        return

    if not payload.startswith(_START_PREFIX):
        await update.message.reply_text("This entry link is invalid.")
        log.error("[ERROR] unknown start payload from user=%s", user.id)
        return

    raw_chat_id = payload[len(_START_PREFIX):]
    try:
        chat_id = int(raw_chat_id)
    except ValueError:
        await update.message.reply_text("This entry link is invalid.")
        log.error("[ERROR] malformed start payload chat id from user=%s", user.id)
        return

    # --- group membership verification (section 16) ---
    try:
        member = await context.bot.get_chat_member(chat_id=chat_id, user_id=user.id)
    except (BadRequest, Forbidden) as exc:
        # User not in the group, group unknown, or bot removed from it.
        log.error(
            "[ERROR] membership check failed user=%s room=%s type=%s",
            user.id,
            chat_id,
            type(exc).__name__,
        )
        await update.message.reply_text(
            "Only members of the Telegram group can enter its 3D room.\n\n"
            "Join the group first — and make sure I am still a member of it — "
            "then tap JOIN ROOM from the group again."
        )
        return
    except TelegramError as exc:
        log.error("[ERROR] membership check error type=%s", type(exc).__name__)
        await update.message.reply_text(
            "Could not verify your group membership right now. Try again."
        )
        return

    is_member = member.status in _MEMBER_OK_STATUSES or (
        member.status == ChatMemberStatus.RESTRICTED
        and getattr(member, "is_member", False)
    )
    if not is_member:
        await update.message.reply_text(
            "Only members of the Telegram group can enter its 3D room."
        )
        log.error("[ERROR] non-member join attempt user=%s room=%s", user.id, chat_id)
        return

    # --- issue signed short-lived join token (section 3) ---
    token = token_service.create_room_token(
        telegram_user_id=user.id,
        chat_id=chat_id,
        first_name=getattr(user, "first_name", None),
        last_name=getattr(user, "last_name", None),
        username=getattr(user, "username", None),
        display_name=display_name,
    )
    join_url = f"{config.FRONTEND_URL}/room/{chat_id}?token={token}"
    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("🎮 JOIN ROOM", url=join_url)]]
    )

    ttl_minutes = max(1, config.ROOM_TOKEN_TTL_SECONDS // 60)
    await update.message.reply_text(
        "🎵 3D MUSIC ROOM\n\n"
        f"Your secure entry link is ready, {display_name}.\n\n"
        "Tap JOIN ROOM below to enter your group's 3D room.",
        reply_markup=keyboard,
    )
    await update.message.reply_text(
        f"This link contains your verified Telegram identity and expires in "
        f"{ttl_minutes} minute(s). Do not share it."
    )
    log.info(
        "[ROOM] join link issued room=%s user=%s display=%r",
        chat_id,
        user.id,
        display_name,
    )


# ---------------------------------------------------------------------------
# /play & /vplay — YouTube search + Firebase playback state
# ---------------------------------------------------------------------------


async def _handle_play(
    update: Update, context: ContextTypes.DEFAULT_TYPE, *, mode: str
) -> None:
    if not update.message or not update.effective_chat or not update.effective_user:
        return

    chat = update.effective_chat
    user = update.effective_user

    if not _is_group_chat(update):
        await update.message.reply_text(
            "Music commands work inside Telegram groups.\n\n"
            "Add me to your group and use them there."
        )
        return

    query = " ".join(context.args).strip() if context.args else ""

    if not query:
        if mode == "video":
            await update.message.reply_text(
                "Please provide a video name.\n\nExample:\n/vplay Shape of You"
            )
        else:  # /play (lyrics, and the legacy audio mode)
            await update.message.reply_text(
                "Please provide a song name.\n\nExample:\n/play Kesariya"
            )
        return

    try:
        await context.bot.send_chat_action(chat_id=chat.id, action=ChatAction.TYPING)
    except TelegramError:
        pass  # cosmetic only

    # --- YouTube search (/play prefers LYRICS versions) ---
    try:
        result = await asyncio.to_thread(
            youtube_service.search_youtube, query, 5, mode == "lyrics"
        )
    except youtube_service.YouTubeServiceError as exc:
        log.error(
            "[ERROR] YouTube search failed chat=%s type=%s", chat.id, type(exc).__name__
        )
        await update.message.reply_text(
            "❌ YouTube search is temporarily unavailable."
        )
        return

    if not result:
        await update.message.reply_text("❌ No matching YouTube result found.")
        return

    display_name = get_display_name(user)

    # --- Add to THIS room's queue (never interrupts what is playing) ---
    try:
        item = room_service.build_queue_item(
            video_id=result["videoId"],
            title=result["title"],
            thumbnail=result.get("thumbnail", ""),
            mode=mode,
            requested_by=user.id,
            requested_by_name=display_name,
            duration_text=result.get("durationText"),
            duration_sec=result.get("durationSec"),
        )
        outcome = await asyncio.to_thread(room_service.enqueue_item, chat.id, item)
    except firebase_exceptions.FirebaseError as exc:
        log.error(
            "[ERROR] Firebase write failed chat=%s type=%s", chat.id, type(exc).__name__
        )
        await update.message.reply_text(
            "❌ Could not update the room right now. Please try again."
        )
        return
    except Exception as exc:  # network/auth surprises must not leak details
        log.error(
            "[ERROR] unexpected playback write failure chat=%s type=%s",
            chat.id,
            type(exc).__name__,
        )
        await update.message.reply_text(
            "❌ Could not update the room right now. Please try again."
        )
        return

    # --- Rich music card (photo + caption + Mini App / control buttons) ---
    join_url = await _join_url(context, chat.id, user, display_name)
    current = outcome["current"]

    if outcome["started"]:
        caption = cards.now_playing_caption(item, outcome["queueLength"])
        keyboard_rows = cards.card_keyboard_rows(
            room_id=chat.id,
            item_id=item["id"],
            paused=False,
            active_card=True,
            join_url=join_url,
        )
    else:
        caption = cards.added_to_queue_caption(
            item, outcome["position"], outcome["queueLength"]
        )
        keyboard_rows = cards.card_keyboard_rows(
            room_id=chat.id,
            item_id=(current or {}).get("id"),
            paused=bool(current) and not current.get("isPlaying", True),
            active_card=False,  # queued card: controls act on the CURRENT song
            join_url=join_url,
        )

    await _reply_card(
        update.message,
        photo_url=result.get("thumbnail") or None,
        caption=caption,
        markup=_to_markup(keyboard_rows),
    )


async def cmd_play(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # /play = LYRICS video on the TV (no more black-screen/audio-only mode)
    await _handle_play(update, context, mode="lyrics")


async def cmd_vplay(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _handle_play(update, context, mode="video")


# ---------------------------------------------------------------------------
# /skip, /stop, /queue — queue control for THIS room only
# ---------------------------------------------------------------------------


async def cmd_skip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_chat:
        return
    if not _is_group_chat(update):
        await update.message.reply_text("Music commands work inside Telegram groups.")
        return

    chat = update.effective_chat
    try:
        state = await asyncio.to_thread(room_service.get_queue_snapshot, chat.id)
        if not state["current"]:
            await update.message.reply_text("Nothing is playing right now.")
            return
        # Same centralized advance logic used by natural video completion.
        outcome = await asyncio.to_thread(room_service.advance_queue, chat.id)
    except Exception as exc:
        log.error(
            "[ERROR] skip failed chat=%s type=%s", chat.id, type(exc).__name__
        )
        await update.message.reply_text(
            "❌ Could not update the room right now. Please try again."
        )
        return

    next_item = outcome.get("next")
    if not next_item:
        await update.message.reply_text("⏹ Queue finished.")
        return

    await update.message.reply_text("⏭ Skipped.")
    await _send_now_playing_card(update, context, chat, next_item)


async def _send_now_playing_card(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    chat,
    item: dict,
) -> None:
    """Post the rich NOW PLAYING card for an item that just became current."""
    user = update.effective_user
    display_name = get_display_name(user) if user else ""
    try:
        state = await asyncio.to_thread(room_service.get_queue_snapshot, chat.id)
        queue_length = len(state["queue"])
    except Exception:
        queue_length = 0
    join_url = (
        await _join_url(context, chat.id, user, display_name) if user else None
    )
    rows = cards.card_keyboard_rows(
        room_id=chat.id,
        item_id=item.get("id"),
        paused=False,
        active_card=True,
        join_url=join_url,
    )
    await _reply_card(
        update.message,
        photo_url=item.get("thumbnail") or None,
        caption=cards.now_playing_caption(item, queue_length),
        markup=_to_markup(rows),
    )


async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_chat:
        return
    if not _is_group_chat(update):
        await update.message.reply_text("Music commands work inside Telegram groups.")
        return

    chat = update.effective_chat
    try:
        await asyncio.to_thread(room_service.stop_playback, chat.id)
    except Exception as exc:
        log.error(
            "[ERROR] stop failed chat=%s type=%s", chat.id, type(exc).__name__
        )
        await update.message.reply_text(
            "❌ Could not update the room right now. Please try again."
        )
        return

    await update.message.reply_text("⏹ Playback stopped and queue cleared.")


async def cmd_queue(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_chat:
        return
    if not _is_group_chat(update):
        await update.message.reply_text("Music commands work inside Telegram groups.")
        return

    chat = update.effective_chat
    try:
        state = await asyncio.to_thread(room_service.get_queue_snapshot, chat.id)
    except Exception as exc:
        log.error(
            "[ERROR] queue read failed chat=%s type=%s", chat.id, type(exc).__name__
        )
        await update.message.reply_text(
            "❌ Could not read the room queue right now. Please try again."
        )
        return

    current = state["current"]
    queue = state["queue"]

    if not current and not queue:
        await update.message.reply_text("The queue is empty. Use /play or /vplay.")
        return

    lines = ["🎶 ROOM QUEUE", ""]
    if current:
        who = (current.get("requestedBy") or {}).get("name", "")
        mode_label = "Lyrics" if current.get("mode") == "lyrics" else "Video"
        lines.append(f"▶️ Now playing: {current.get('title', '')}")
        lines.append(f"    {mode_label} · requested by {who}")
    else:
        lines.append("▶️ Nothing is playing")

    if queue:
        lines.append("")
        lines.append("Next up:")
        for index, item in enumerate(queue[:10], start=1):
            who = (item.get("requestedBy") or {}).get("name", "")
            mode_label = "Lyrics" if item.get("mode") == "lyrics" else "Video"
            lines.append(f"{index}. {item.get('title', '')}")
            lines.append(f"    {mode_label} · requested by {who}")
        if len(queue) > 10:
            lines.append(f"…and {len(queue) - 10} more")

    await update.message.reply_text("\n".join(lines))


async def cmd_pause(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_chat:
        return
    if not _is_group_chat(update):
        await update.message.reply_text("Music commands work inside Telegram groups.")
        return
    chat = update.effective_chat
    try:
        paused = await asyncio.to_thread(room_service.pause_playback, chat.id)
    except Exception as exc:
        log.error("[ERROR] pause failed chat=%s type=%s", chat.id, type(exc).__name__)
        await update.message.reply_text(
            "❌ Could not update the room right now. Please try again."
        )
        return
    if paused:
        await update.message.reply_text("⏸ Playback paused.")
    else:
        await update.message.reply_text("Nothing is playing right now.")


async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_chat:
        return
    if not _is_group_chat(update):
        await update.message.reply_text("Music commands work inside Telegram groups.")
        return
    chat = update.effective_chat
    try:
        resumed = await asyncio.to_thread(room_service.resume_playback, chat.id)
    except Exception as exc:
        log.error("[ERROR] resume failed chat=%s type=%s", chat.id, type(exc).__name__)
        await update.message.reply_text(
            "❌ Could not update the room right now. Please try again."
        )
        return
    if resumed:
        await update.message.reply_text("▶️ Playback resumed.")
    else:
        await update.message.reply_text("Nothing is paused right now.")


# ---------------------------------------------------------------------------
# Callback queries — DM panels (ui:*) and room controls (room_*)
# ---------------------------------------------------------------------------

_MEMBER_OK_SET = {ChatMemberStatus.OWNER, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.MEMBER}


async def _answer(query, text: str = "", alert: bool = False) -> None:
    try:
        await query.answer(text=text[:190], show_alert=alert)
    except TelegramError:
        pass


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.data:
        return
    data = query.data
    try:
        if data in ("ui:home", "ui:commands"):
            await _handle_panel_callback(update, context, data)
        elif data.startswith("room_"):
            await _handle_room_callback(update, context, data)
        else:
            await _answer(query, "Unknown action.")
    except TelegramError as exc:
        log.error("[ERROR] callback failed data-kind=%s type=%s", data.split(":")[0], type(exc).__name__)
        await _answer(query, "Something went wrong. Try again.")


async def _handle_panel_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE, data: str
) -> None:
    """DM panels: 📖 Commands and ← Back (message edit, no new messages)."""
    query = update.callback_query
    message = query.message
    if not message or (message.chat and message.chat.type != ChatType.PRIVATE):
        await _answer(query)
        return

    if data == "ui:commands":
        rows = [[{"text": "← Back", "callback_data": "ui:home"}]]
        caption = _COMMAND_PANEL
        log.info("[ROOM] commands panel opened user=%s", query.from_user.id)
    else:  # ui:home -> rebuild the welcome panel
        user = query.from_user
        display_name = get_display_name(user)
        open_room_url = await _join_url(context, message.chat.id, user, display_name)
        try:
            bot_username = await _bot_username(context)
        except TelegramError:
            bot_username = None
        rows = []
        if open_room_url:
            rows.append([{"text": "🚪 Open Room", "url": open_room_url}])
        if bot_username:
            rows.append([{"text": "➕ Add to Group", "url": f"https://t.me/{bot_username}?startgroup=true"}])
        rows.append([{"text": "📖 Commands", "callback_data": "ui:commands"}])
        community = []
        if config.CHANNEL_URL:
            community.append({"text": "📢 Channel", "url": config.CHANNEL_URL})
        if config.SUPPORT_URL:
            community.append({"text": "💬 Support", "url": config.SUPPORT_URL})
        if community:
            rows.append(community)
        caption = _WELCOME_CAPTION

    markup = _to_markup(rows)
    try:
        await message.edit_message_caption(
            caption=caption, reply_markup=markup, parse_mode=ParseMode.HTML
        )
    except BadRequest:
        # Fallback when the original panel was sent as plain text.
        try:
            await message.edit_message_text(
                caption, reply_markup=markup, parse_mode=ParseMode.HTML
            )
        except BadRequest:
            pass
    await _answer(query)


# ---------------------------------------------------------------------------
# Room playback control callbacks: room_pause / room_resume / room_skip / room_queue
# ---------------------------------------------------------------------------


def _parse_room_callback(data: str):
    """room_<action>:<room_id>[:<item_id>[:c]] -> (action, room_id, item_id, is_active_card)"""
    parts = data.split(":")
    if len(parts) < 2:
        return None
    action = parts[0]
    if action not in ("room_pause", "room_resume", "room_skip", "room_queue"):
        return None
    try:
        room_id = int(parts[1])
    except ValueError:
        return None
    item_id = parts[2] if len(parts) >= 3 and parts[2] else None
    is_active = len(parts) >= 4 and parts[3] == "c"
    return action, room_id, item_id, is_active


async def _callback_member_ok(context: ContextTypes.DEFAULT_TYPE, room_id: int, user_id: int) -> bool | None:
    """True=member, False=not a member, None=check unavailable."""
    try:
        member = await context.bot.get_chat_member(chat_id=room_id, user_id=user_id)
    except (BadRequest, Forbidden):
        return False
    except TelegramError:
        return None
    if member.status in _MEMBER_OK_SET:
        return True
    return bool(member.status == ChatMemberStatus.RESTRICTED and getattr(member, "is_member", False))


async def _handle_room_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE, data: str
) -> None:
    query = update.callback_query
    message = query.message
    parsed = _parse_room_callback(data)
    if parsed is None:
        await _answer(query, "Invalid control.")
        return
    action, room_id, item_id, is_active_card = parsed

    # Room binding: the button only works in the group where the card lives.
    if not message or not message.chat or message.chat.id != room_id:
        log.error("[ERROR] cross-room callback attempt data-room=%s", room_id)
        await _answer(query, "This control doesn't belong to this chat.")
        return

    # Permission: only members of THAT group may control its room.
    member_state = await _callback_member_ok(context, room_id, query.from_user.id)
    if member_state is None:
        await _answer(query, "Could not verify your membership. Try again.")
        return
    if not member_state:
        await _answer(query, "Only group members can control this room.")
        return

    try:
        state = await asyncio.to_thread(room_service.get_queue_snapshot, room_id)
    except Exception as exc:
        log.error("[ERROR] callback state read failed room=%s type=%s", room_id, type(exc).__name__)
        await _answer(query, "Backend is busy. Try again.")
        return

    current = state["current"]
    queue_length = state["queueLength"]

    if action == "room_queue":
        await _answer(query, cards.queue_alert_text(state), alert=True)
        return

    if item_id and (not current or current.get("id") != item_id):
        await _answer(query, "That song already finished or was skipped.")
        return

    if action == "room_pause":
        ok = await asyncio.to_thread(room_service.pause_playback, room_id)
        if not ok:
            await _answer(query, "Nothing is playing.")
            return
        await _answer(query, "⏸ Paused")
        if is_active_card:
            await _refresh_card(
                update, context, message, room_id, current,
                cards.paused_caption(current, queue_length), paused=True,
            )
        return

    if action == "room_resume":
        ok = await asyncio.to_thread(room_service.resume_playback, room_id)
        if not ok:
            await _answer(query, "Nothing is paused.")
            return
        await _answer(query, "▶️ Resumed")
        if is_active_card:
            await _refresh_card(
                update, context, message, room_id, current,
                cards.now_playing_caption(current, queue_length), paused=False,
            )
        return

    if action == "room_skip":
        outcome = await asyncio.to_thread(
            room_service.advance_queue, room_id, item_id or None
        )
        if outcome["stale"]:
            await _answer(query, "⏭ Already skipped.")
            return
        next_item = outcome.get("next")
        previous = outcome.get("previous") or current
        if not next_item:
            await _answer(query, "⏹ Queue finished.")
            if is_active_card and previous:
                await _edit_card_caption(
                    message,
                    cards.finished_caption(previous),
                    cards.ended_card_keyboard_rows(
                        room_id=room_id,
                        join_url=await _join_url(
                            context, room_id, query.from_user,
                            get_display_name(query.from_user),
                        ),
                    ),
                )
            return
        await _answer(query, f"⏭ Skipped. Now: {next_item.get('title', '')[:40]}")
        if is_active_card and previous:
            await _edit_card_caption(
                message,
                cards.skipped_caption(previous),
                cards.ended_card_keyboard_rows(
                    room_id=room_id,
                    join_url=await _join_url(
                        context, room_id, query.from_user,
                        get_display_name(query.from_user),
                    ),
                ),
            )
        # Post ONE fresh card for the new current item (no spam, no edits lost)
        join_url = await _join_url(
            context, room_id, query.from_user, get_display_name(query.from_user)
        )
        rows = cards.card_keyboard_rows(
            room_id=room_id,
            item_id=next_item.get("id"),
            paused=False,
            active_card=True,
            join_url=join_url,
        )
        await _send_group_card(
            context, room_id,
            photo_url=next_item.get("thumbnail") or None,
            caption=cards.now_playing_caption(next_item, outcome["queueLength"]),
            rows=rows,
        )
        return


async def _send_group_card(
    context: ContextTypes.DEFAULT_TYPE, chat_id, *, photo_url, caption, rows
) -> None:
    markup = _to_markup(rows)
    try:
        if photo_url:
            await context.bot.send_photo(
                chat_id=chat_id, photo=photo_url, caption=caption,
                reply_markup=markup, parse_mode=ParseMode.HTML,
            )
            return
        raise TelegramError("no photo url")
    except TelegramError:
        await context.bot.send_message(
            chat_id=chat_id, text=caption,
            reply_markup=markup, parse_mode=ParseMode.HTML,
        )


async def _edit_card_caption(message, caption: str, rows) -> None:
    markup = _to_markup(rows)
    try:
        await message.edit_message_caption(
            caption=caption, reply_markup=markup, parse_mode=ParseMode.HTML
        )
    except BadRequest:
        try:
            await message.edit_message_text(
                caption, reply_markup=markup, parse_mode=ParseMode.HTML
            )
        except BadRequest:
            pass


async def _refresh_card(
    update, context, message, room_id, item, caption, *, paused: bool
) -> None:
    join_url = await _join_url(
        context, room_id, update.callback_query.from_user,
        get_display_name(update.callback_query.from_user),
    )
    rows = cards.card_keyboard_rows(
        room_id=room_id,
        item_id=item.get("id"),
        paused=paused,
        active_card=True,
        join_url=join_url,
    )
    await _edit_card_caption(message, caption, rows)


# ---------------------------------------------------------------------------
# Global error handler — log types only, never secret material
# ---------------------------------------------------------------------------


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.error(
        "[ERROR] handler error type=%s",
        type(context.error).__name__ if context.error else "unknown",
    )


# ---------------------------------------------------------------------------
# Application factory
# ---------------------------------------------------------------------------


def build_application() -> Application:
    if not config.TELEGRAM_BOT_TOKEN:
        raise config.ConfigError("TELEGRAM_BOT_TOKEN is not configured.")

    application = (
        Application.builder().token(config.TELEGRAM_BOT_TOKEN).build()
    )

    application.add_handler(CommandHandler("room", cmd_room))
    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("play", cmd_play))
    application.add_handler(CommandHandler("vplay", cmd_vplay))
    application.add_handler(CommandHandler("pause", cmd_pause))
    application.add_handler(CommandHandler("resume", cmd_resume))
    application.add_handler(CommandHandler("skip", cmd_skip))
    application.add_handler(CommandHandler("stop", cmd_stop))
    application.add_handler(CommandHandler("queue", cmd_queue))
    application.add_handler(CallbackQueryHandler(on_callback))
    application.add_error_handler(on_error)

    return application
