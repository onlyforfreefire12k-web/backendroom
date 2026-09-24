"""
bot.py
------
Telegram bot for the 3D Music Room.

Commands
--------
/room                 (groups) post the JOIN ROOM card for this group
/play <song name>     (groups) play music  -> playbackMode = "audio"
/vplay <song name>    (groups) play video  -> playbackMode = "video"

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

from firebase_admin import exceptions as firebase_exceptions
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction, ChatMemberStatus, ChatType
from telegram.error import BadRequest, Forbidden, TelegramError
from telegram.ext import Application, CommandHandler, ContextTypes

import config
import room_service
import token_service
import youtube_service

log = logging.getLogger("backend.bot")

_START_PREFIX = "room_"

_GROUP_CHAT_TYPES = (ChatType.GROUP, ChatType.SUPERGROUP)

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
    deep_link = f"https://t.me/{bot_username}?start={_START_PREFIX}{room_id}"
    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("🎮 JOIN ROOM", url=deep_link)]]
    )

    await update.message.reply_text(
        "🎵 3D MUSIC ROOM\n\n"
        "Join the group's 3D room.\n\n"
        "Tap JOIN ROOM, then press START — you will receive your personal "
        "secure entry link for this group's room.",
        reply_markup=keyboard,
    )
    log.info("[ROOM] room requested chat=%s by user=%s", room_id, user.id)


# ---------------------------------------------------------------------------
# /start room_<chatId> — verify membership and issue the signed join URL
# ---------------------------------------------------------------------------


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user:
        return

    user = update.effective_user
    display_name = get_display_name(user)
    payload = (context.args[0] if context.args else "").strip()

    if not payload:
        await update.message.reply_text(
            "🎵 3D MUSIC ROOM\n\n"
            "To join your group's room, send /room inside the group and tap "
            "the JOIN ROOM button."
        )
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
        if mode == "audio":
            await update.message.reply_text(
                "Please provide a song name.\n\nExample:\n/play Kesariya"
            )
        else:
            await update.message.reply_text(
                "Please provide a video name.\n\nExample:\n/vplay Shape of You"
            )
        return

    try:
        await context.bot.send_chat_action(chat_id=chat.id, action=ChatAction.TYPING)
    except TelegramError:
        pass  # cosmetic only

    # --- YouTube search (sections 7/9/10) ---
    try:
        result = await asyncio.to_thread(youtube_service.search_youtube, query)
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

    # --- Firebase playback state (sections 7/9/11) ---
    try:
        await asyncio.to_thread(
            room_service.set_playback_state,
            chat.id,
            video_id=result["videoId"],
            title=result["title"],
            thumbnail=result["thumbnail"],
            mode=mode,
            requested_by=user.id,
            requested_by_name=display_name,
        )
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

    requested_by_label = _requested_by_label(user, display_name)

    if mode == "audio":
        text = (
            "🎵 Now playing\n\n"
            f"{result['title']}\n\n"
            "Mode: Music\n\n"
            f"Requested by: {requested_by_label}"
        )
    else:
        text = (
            "📺 Now playing video\n\n"
            f"{result['title']}\n\n"
            "Mode: Video\n\n"
            f"Requested by: {requested_by_label}"
        )

    await update.message.reply_text(text)


async def cmd_play(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _handle_play(update, context, mode="audio")


async def cmd_vplay(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _handle_play(update, context, mode="video")


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
    application.add_error_handler(on_error)

    return application
