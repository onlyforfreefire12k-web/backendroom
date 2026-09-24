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
POST /api/token/validate      -> verifies a signed room token (legacy browser flow)
POST /api/miniapp/exchange    -> Telegram Mini App initData + launch code -> verified room session
GET  /api/rooms/<roomId>/state -> read-only playback state for a room
"""

from __future__ import annotations

import logging
import re
import secrets
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

# startapp payloads only allow A-Za-z0-9_- (max 512); our codes are 12 chars.
_LAUNCH_CODE_PATTERN = re.compile(r"^[A-Za-z0-9_-]{8,64}$")

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
# Mini App session exchange (Telegram Mini App launch flow)
#
# The Mini App posts its raw Telegram initData. We:
#   1. verify Telegram's HMAC signature  -> VERIFIED Telegram identity
#   2. extract the startapp launch code  -> resolves the ROOM (group chat id)
#   3. re-check group membership via getChatMember
#   4. mint a signed session token (existing ROOM_TOKEN_SECRET mechanism)
# Nothing sensitive ever travels inside the Mini App URL itself.
# ---------------------------------------------------------------------------


def _error_response(code: str, http_status: int):
    return jsonify({"ok": False, "error": code}), http_status, _JSON_HEADERS


@app.post("/api/miniapp/exchange")
def miniapp_exchange():
    import launch_service
    import tg_auth
    import token_service

    body = request.get_json(silent=True) or {}
    init_data = body.get("initData") or ""

    if not init_data:
        log.error("[ERROR] miniapp exchange without init_data")
        return _error_response("missing_init_data", 400)

    # 1) verified Telegram identity (signature + freshness)
    try:
        data = tg_auth.validate_init_data(init_data)
    except tg_auth.InitDataExpiredError:
        log.error("[ERROR] expired init_data presented")
        return _error_response("expired_init_data", 401)
    except tg_auth.InitDataInvalidError as exc:
        log.error("[ERROR] invalid init_data presented: %s", exc)
        return _error_response("invalid_init_data", 401)

    user = data.get("user") or {}
    telegram_id = user.get("id")
    if not isinstance(telegram_id, int):
        log.error("[ERROR] init_data without user object")
        return _error_response("missing_user", 401)

    # 2) launch code -> room
    start_param = (data.get("start_param") or "").strip()
    if not start_param:
        log.error("[ERROR] miniapp exchange user=%s without launch code", telegram_id)
        return _error_response("missing_launch_code", 400)
    if not _LAUNCH_CODE_PATTERN.match(start_param):
        log.error("[ERROR] malformed launch code presented user=%s", telegram_id)
        return _error_response("invalid_launch_code", 404)

    try:
        launch = launch_service.resolve_launch_code(start_param)
    except launch_service.LaunchCodeExpiredError:
        log.error("[ERROR] expired launch code presented user=%s", telegram_id)
        return _error_response("expired_launch_code", 410)
    except launch_service.LaunchCodeInvalidError:
        log.error("[ERROR] invalid launch code presented user=%s", telegram_id)
        return _error_response("invalid_launch_code", 404)
    except Exception as exc:
        log.error("[ERROR] launch code lookup failed type=%s", type(exc).__name__)
        return _error_response("internal_error", 500)

    room_id = str(launch["roomId"])

    # 3) group membership (room isolation: members of THIS group only)
    try:
        is_member = tg_auth.verify_room_membership(room_id, telegram_id)
    except tg_auth.MembershipCheckError as exc:
        log.error(
            "[ERROR] membership check unavailable room=%s user=%s type=%s",
            room_id,
            telegram_id,
            type(exc).__name__,
        )
        return _error_response("membership_check_failed", 503)
    if not is_member:
        log.error(
            "[ERROR] not-a-member exchange attempt room=%s user=%s", room_id, telegram_id
        )
        return _error_response("not_room_member", 403)

    # 4) signed room session via the existing ROOM_TOKEN_SECRET mechanism
    display_name = tg_auth.display_name_from_user(user)
    try:
        session_token = token_service.create_room_token(
            telegram_user_id=telegram_id,
            chat_id=int(room_id),
            first_name=user.get("first_name"),
            last_name=user.get("last_name"),
            username=user.get("username"),
            display_name=display_name,
            extra={"source": "miniapp", "sessionId": secrets.token_hex(8)},
        )
    except Exception as exc:
        log.error("[ERROR] session issue failed type=%s", type(exc).__name__)
        return _error_response("internal_error", 500)

    log.info(
        "[AUTH] miniapp exchange ok room=%s telegramId=%s display=%r",
        room_id,
        telegram_id,
        display_name,
    )

    return (
        jsonify(
            {
                "ok": True,
                "telegramId": telegram_id,
                "firstName": user.get("first_name") or "",
                "lastName": user.get("last_name") or "",
                "username": user.get("username") or "",
                "displayName": display_name,
                "roomId": room_id,
                "sessionToken": session_token,
                "expiresIn": config.ROOM_TOKEN_TTL_SECONDS,
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
