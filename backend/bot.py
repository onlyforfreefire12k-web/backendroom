import logging
from threading import Thread

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CommandHandler, ContextTypes

import config
import room_service
import youtube_service
from room_service import get_display_name


logger = logging.getLogger(__name__)

_bot_thread = None


def _is_group(update: Update) -> bool:
    chat = update.effective_chat
    return chat is not None and chat.type in ("group", "supergroup")


def _user_handle(user):
    if getattr(user, "username", None):
        return f"@{user.username}"
    return get_display_name(user)


async def _room_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    if message is None:
        return

    if not _is_group(update):
        await message.reply_text("This command can only be used inside a Telegram group.")
        return

    user = update.effective_user
    if user is None:
        await message.reply_text("Could not identify your Telegram account.")
        return

    chat_id = update.effective_chat.id
    token = room_service.create_room_token(user, chat_id)
    url = f"{config.FRONTEND_URL}/room/{chat_id}?token={token}"

    keyboard = [[InlineKeyboardButton("🎮 JOIN ROOM", url=url)]]
    reply_markup = InlineKeyboardMarkup(keyboard)



    await message.reply_text(
        "🎵 3D MUSIC ROOM\n\nJoin your group's 3D room.",
        reply_markup=reply_markup,
    )

    logger.info("[ROOM] room requested: %s", chat_id)


async def _play_command(update: Update, context: ContextTypes.DEFAULT_TYPE, mode: str):
    message = update.effective_message
    if message is None:
        return

    if not _is_group(update):
        await message.reply_text("This command can only be used inside a Telegram group.")
        return

    user = update.effective_user
    if user is None:
        await message.reply_text("Could not identify your Telegram account.")
        return

    query = " ".join(context.args or []).strip()

    if not query:
        if mode == "audio":
            await message.reply_text("Please provide a song name.\n\nExample:\n/play Kesariya")
        else:
            await message.reply_text("Please provide a video name.\n\nExample:\n/vplay Shape of You")
        return

    if len(query) > 300:
        await message.reply_text("Please use a shorter search query.")
        return

    chat_id = update.effective_chat.id

    try:
        result = youtube_service.search_youtube(query)
    except youtube_service.YouTubeServiceError:
        await message.reply_text("❌ YouTube search is temporarily unavailable.")
        return

    if not result:
        await message.reply_text("❌ No matching YouTube result found.")
        return

    playback_mode = "audio" if mode == "audio" else "video"

    try:
        room_service.set_playback_state(
            room_id=chat_id,
            video_id=result["videoId"],
            title=result["title"],
            thumbnail=result["thumbnail"],
            playback_mode=playback_mode,
            requested_by=user.id,
            requested_by_name=get_display_name(user),
        )
    except Exception:
        logger.exception("[ERROR] Firebase update failed")
        await message.reply_text("❌ Could not save playback state. Please try again.")
        return

    handle = _user_handle(user)

    if mode == "audio":
        response = (
            f"🎵 Now playing\n\n"
            f"{result['title']}\n\n"
            f"Mode: Music\n"
            f"Requested by: {handle}"
        )
    else:
        response = (
            f"📺 Now playing video\n\n"
            f"{result['title']}\n\n"
            f"Mode: Video\n"
            f"Requested by: {handle}"
        )

    await message.reply_text(response)

    logger.info("[PLAY] /%s handled for room %s", "play" if mode == "audio" else "vplay", chat_id)


async def cmd_play(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _play_command(update, context, "audio")


async def cmd_vplay(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _play_command(update, context, "video")


async def _error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("[ERROR] Telegram update error: %s", type(context.error).__name_)


def _run_bot():
    try:
        application = Application.builder().token(config.TELEGRAM_BOT_TOKEN).build()

        application.add_handler(CommandHandler("room", _room_command))
        application.add_handler(CommandHandler("play", cmd_play))
        application.add_handler(CommandHandler("vplay", cmd_vplay))

        application.add_error_handler(_error_handler)

        application.run_polling()
    except Exception:
        logger.exception("[ERROR] Telegram bot stopped")


def start_bot_in_background():
    global _bot_thread

    if _bot_thread is not None and _bot_thread.is_alive():
        return

    _bot_thread = Thread(target=_run_bot, name="telegram-bot", daemon=True)
    _bot_thread.start()
