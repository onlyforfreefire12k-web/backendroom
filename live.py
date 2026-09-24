"""
live.py
-------
Production entry point — `python live.py`

Runs BOTH processes together in one dyno/web-service:

1. The Telegram bot (long polling, python-telegram-bot) — main thread.
2. The Flask HTTP server (health + token validation API) — daemon thread
   bound to 0.0.0.0:$PORT so Render's health checks pass.

No gunicorn — Flask's server is exactly what Render needs here because the
HTTP surface is only health checks + the small join-token API.

Endpoints
---------
GET  /                        -> "3D Room Backend Online"
GET  /health                  -> {"status": "ok", "service": "3d-room-backend"}
POST /api/token/validate      -> verifies a signed room token, returns identity
GET  /api/rooms/<roomId>/state -> read-only playback state for a room
"""

from __future__ import annotations

import logging
import re
import threading

from flask import Flask, jsonify, request

import config

logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("backend.live")

app = Flask(__name__)

_ROOM_ID_PATTERN = re.compile(r"^-?\d+$")

_JSON_HEADERS = {"Cache-Control": "no-store"}


# ---------------------------------------------------------------------------
# CORS (only for the frontend origin, only for /api/*)
# ---------------------------------------------------------------------------


@app.before_request
def _handle_preflight():
    if request.method == "OPTIONS" and request.path.startswith("/api/"):
        return ("", 204)
    return None


@app.after_request
def _apply_cors(response):
    if request.path.startswith("/api/"):
        response.headers["Access-Control-Allow-Origin"] = config.FRONTEND_URL or "*"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type"
        response.headers["Access-Control-Max-Age"] = "600"
    return response


# ---------------------------------------------------------------------------
# Health endpoints
# ---------------------------------------------------------------------------


@app.get("/")
def index():
    return "3D Room Backend Online"


@app.get("/health")
def health():
    # Must stay lightweight and dependency-free — Render probes this.
    return jsonify({"status": "ok", "service": "3d-room-backend"}), 200


# ---------------------------------------------------------------------------
# Room-token validation (section 3)
# ---------------------------------------------------------------------------


@app.post("/api/token/validate")
def validate_token():
    import token_service  # local import keeps module list explicit

    body = request.get_json(silent=True) or {}
    token = body.get("token", "")

    try:
        payload = token_service.validate_room_token(token)
    except token_service.TokenExpiredError:
        log.error("[ERROR] expired room token presented")
        return (
            jsonify({"ok": False, "error": "expired_token"}),
            401,
            _JSON_HEADERS,
        )
    except token_service.TokenInvalidError as exc:
        log.error("[ERROR] invalid room token presented: %s", exc)
        return (
            jsonify({"ok": False, "error": "invalid_token"}),
            401,
            _JSON_HEADERS,
        )

    log.info(
        "[AUTH] room token validated room=%s telegramId=%s display=%r",
        payload["chatId"],
        payload["telegramUserId"],
        payload.get("displayName", ""),
    )

    return (
        jsonify(
            {
                "ok": True,
                "telegramId": payload["telegramUserId"],
                "firstName": payload.get("firstName", ""),
                "lastName": payload.get("lastName", ""),
                "username": payload.get("username", ""),
                "displayName": payload.get("displayName", ""),
                "roomId": payload["chatId"],
            }
        ),
        200,
        _JSON_HEADERS,
    )


# ---------------------------------------------------------------------------
# Read-only room state (helps the frontend sync before its Firebase
# listener attaches; browsers can also read Firebase directly)
# ---------------------------------------------------------------------------


@app.get("/api/rooms/<room_id>/state")
def room_state(room_id: str):
    import firebase_service  # local import: initialized lazily on first use
    import room_service
    from firebase_admin import exceptions as firebase_exceptions

    if not _ROOM_ID_PATTERN.match(room_id):
        return jsonify({"ok": False, "error": "invalid_room_id"}), 400

    try:
        firebase_service.init_firebase()
        metadata = room_service.get_metadata(room_id)
    except firebase_exceptions.FirebaseError as exc:
        log.error("[ERROR] Firebase read failed room=%s type=%s", room_id, type(exc).__name__)
        return jsonify({"ok": False, "error": "internal_error"}), 500
    except Exception as exc:
        log.error("[ERROR] room state read failed room=%s type=%s", room_id, type(exc).__name__)
        return jsonify({"ok": False, "error": "internal_error"}), 500

    return (
        jsonify(
            {
                "ok": True,
                "roomId": str(room_id),
                "exists": metadata is not None,
                "metadata": metadata,
            }
        ),
        200,
        _JSON_HEADERS,
    )


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------


def _run_flask() -> None:
    log.info("[LIVE] Flask listening on 0.0.0.0:%s", config.PORT)
    app.run(
        host="0.0.0.0",
        port=config.PORT,
        threaded=True,
        use_reloader=False,
        debug=False,
    )


def main() -> None:
    config.validate_or_raise()

    import bot as bot_module
    import firebase_service

    firebase_service.init_firebase()
    application = bot_module.build_application()

    flask_thread = threading.Thread(
        target=_run_flask, name="flask-health-server", daemon=True
    )
    flask_thread.start()

    log.info("[LIVE] Telegram bot starting (long polling)…")
    # run_polling manages its own asyncio loop and signal handlers; it must
    # run on the main thread. drop_pending_updates skips stale updates after
    # restarts so a rebooted dyno never replays old songs.
    application.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
