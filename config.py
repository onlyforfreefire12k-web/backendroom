"""
config.py
---------
Central configuration for the 3D Telegram Music Room backend.

Every value is read from environment variables (Render dashboard).
No secrets are ever hard-coded and no secret value is ever logged.
"""

from __future__ import annotations

import logging
import os

log = logging.getLogger("backend.config")


class ConfigError(RuntimeError):
    """Raised when required environment variables are missing/invalid."""


# ---------------------------------------------------------------------------
# Required environment variables
# ---------------------------------------------------------------------------

REQUIRED_ENV_VARS = (
    "TELEGRAM_BOT_TOKEN",
    "YOUTUBE_API_KEY",
    "FIREBASE_PROJECT_ID",
    "FIREBASE_CLIENT_EMAIL",
    "FIREBASE_PRIVATE_KEY",
    "FIREBASE_DATABASE_URL",
    "FRONTEND_URL",
    "ROOM_TOKEN_SECRET",
    "MINI_APP_SHORT_NAME",
)

DEFAULT_PORT = 10000
DEFAULT_ROOM_TOKEN_TTL_SECONDS = 3600  # 1 hour
DEFAULT_LAUNCH_CODE_TTL_SECONDS = 1800  # 30 minutes
DEFAULT_WEBAPP_INITDATA_MAX_AGE_SECONDS = 86400  # 24 hours


def _env(name: str) -> str | None:
    value = os.environ.get(name)
    if value is None:
        return None
    value = value.strip()
    return value or None


def _normalize_private_key(raw: str) -> str:
    """
    Render (and most dashboards) store multi-line PEM keys with literal
    "\\n" sequences. Convert them back to real newlines and strip any
    wrapping quotes that dashboards sometimes keep.
    """
    key = raw.strip().strip('"').strip("'")
    return key.replace("\\n", "\n")


# ---------------------------------------------------------------------------
# Public configuration values (safe to import anywhere)
# ---------------------------------------------------------------------------

TELEGRAM_BOT_TOKEN = _env("TELEGRAM_BOT_TOKEN")
YOUTUBE_API_KEY = _env("YOUTUBE_API_KEY")

FIREBASE_PROJECT_ID = _env("FIREBASE_PROJECT_ID")
FIREBASE_CLIENT_EMAIL = _env("FIREBASE_CLIENT_EMAIL")
_raw_private_key = _env("FIREBASE_PRIVATE_KEY")
FIREBASE_PRIVATE_KEY = (
    _normalize_private_key(_raw_private_key) if _raw_private_key else None
)

# Realtime Database URL — ALWAYS taken from the FIREBASE_DATABASE_URL
# environment variable. It is REQUIRED: RTDB URLs are region-specific
# (e.g. https://room-131c2-default-rtdb.asia-southeast1.firebasedatabase.app),
# so the code must never guess a "<project>-default-rtdb.firebaseio.com"
# style URL. The exact URL is visible in the Firebase console under
# Realtime Database. A trailing slash is tolerated and stripped.
FIREBASE_DATABASE_URL = (_env("FIREBASE_DATABASE_URL") or "").rstrip("/") or None

FRONTEND_URL = (_env("FRONTEND_URL") or "").rstrip("/") or None

ROOM_TOKEN_SECRET = _env("ROOM_TOKEN_SECRET")

# Render injects PORT automatically; default safely to 10000 when unset
# or malformed (e.g. local runs without the variable).
try:
    PORT = int(os.environ.get("PORT") or str(DEFAULT_PORT))
except (TypeError, ValueError):
    PORT = DEFAULT_PORT

try:
    ROOM_TOKEN_TTL_SECONDS = int(
        os.environ.get("ROOM_TOKEN_TTL_SECONDS") or str(DEFAULT_ROOM_TOKEN_TTL_SECONDS)
    )
except (TypeError, ValueError):
    ROOM_TOKEN_TTL_SECONDS = DEFAULT_ROOM_TOKEN_TTL_SECONDS

# --- Telegram Mini App (room launch inside Telegram) ----------------------------
# The Mini App short name registered with @BotFather (/newapp). Direct links:
#   https://t.me/<bot_username>/<MINI_APP_SHORT_NAME>?startapp=<launch_code>
MINI_APP_SHORT_NAME = _env("MINI_APP_SHORT_NAME")

try:
    LAUNCH_CODE_TTL_SECONDS = int(
        os.environ.get("LAUNCH_CODE_TTL_SECONDS")
        or str(DEFAULT_LAUNCH_CODE_TTL_SECONDS)
    )
except (TypeError, ValueError):
    LAUNCH_CODE_TTL_SECONDS = DEFAULT_LAUNCH_CODE_TTL_SECONDS

try:
    WEBAPP_INITDATA_MAX_AGE_SECONDS = int(
        os.environ.get("WEBAPP_INITDATA_MAX_AGE_SECONDS")
        or str(DEFAULT_WEBAPP_INITDATA_MAX_AGE_SECONDS)
    )
except (TypeError, ValueError):
    WEBAPP_INITDATA_MAX_AGE_SECONDS = DEFAULT_WEBAPP_INITDATA_MAX_AGE_SECONDS

# Optional community links for the DM welcome UI — the buttons are hidden
# entirely when these are not configured (no fake/placeholder URLs are used).
CHANNEL_URL = _env("CHANNEL_URL")
SUPPORT_URL = _env("SUPPORT_URL")

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()


def missing_env_vars() -> list[str]:
    """Return the list of required environment variables that are not set."""
    missing: list[str] = []
    for name in REQUIRED_ENV_VARS:
        if globals().get(name) in (None, ""):
            missing.append(name)
    return missing


def validate_or_raise() -> None:
    """Fail fast at startup if anything required is missing."""
    missing = missing_env_vars()
    if missing:
        raise ConfigError(
            "Missing required environment variables: "
            + ", ".join(sorted(missing))
        )
    log.info(
        "[LIVE] Config ok — port=%s token_ttl=%ss frontend_url_set=%s",
        PORT,
        ROOM_TOKEN_TTL_SECONDS,
        bool(FRONTEND_URL),
    )
