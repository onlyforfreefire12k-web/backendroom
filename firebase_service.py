"""
firebase_service.py
-------------------
Firebase Admin SDK initialization (singleton) for the Realtime Database.

- The backend uses ONLY the Firebase Admin SDK (never the client SDK).
- Credentials come from environment variables (see config.py):
  FIREBASE_PROJECT_ID / FIREBASE_CLIENT_EMAIL / FIREBASE_PRIVATE_KEY.
- The private key newline conversion ("\\n" -> real newline) is handled
  in config.py so the key can be pasted safely into the Render dashboard.
"""

from __future__ import annotations

import logging
import threading

import firebase_admin
from firebase_admin import credentials, db as rtdb

import config

log = logging.getLogger("backend.firebase")

_lock = threading.Lock()
_initialized = False


def init_firebase() -> None:
    """
    Initialize the Firebase Admin app exactly once (thread-safe).
    Safe to call from any module/thread — subsequent calls are no-ops.
    """
    global _initialized
    with _lock:
        if _initialized or firebase_admin._apps:  # noqa: SLF001 (sdk-internal check)
            _initialized = True
            return

        if not (
            config.FIREBASE_PROJECT_ID
            and config.FIREBASE_CLIENT_EMAIL
            and config.FIREBASE_PRIVATE_KEY
        ):
            raise config.ConfigError(
                "Firebase credentials are not configured (see REQUIRED_ENV_VARS)."
            )

        service_account = {
            "type": "service_account",
            "project_id": config.FIREBASE_PROJECT_ID,
            "private_key": config.FIREBASE_PRIVATE_KEY,
            "client_email": config.FIREBASE_CLIENT_EMAIL,
            "token_uri": "https://oauth2.googleapis.com/token",
        }

        cred = credentials.Certificate(service_account)
        firebase_admin.initialize_app(
            cred,
            {"databaseURL": config.FIREBASE_DATABASE_URL},
        )
        _initialized = True
        log.info(
            "[LIVE] Firebase Admin initialized (project configured, rtdb url set)."
        )


def get_ref(path: str):
    """Return a Realtime Database reference, initializing Firebase if needed."""
    init_firebase()
    return rtdb.reference(path)


def server_timestamp() -> dict:
    """
    Firebase server timestamp placeholder.
    Stored by RTDB as the current server time in milliseconds — this is the
    shared 'startedAt' clock used to synchronize late-joining browsers.
    """
    return {".sv": "timestamp"}
