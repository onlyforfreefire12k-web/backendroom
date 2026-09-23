import logging
import os

from flask import Flask, jsonify, request

import config
import room_service
from bot import start_bot_in_background
from firebase_service import init_firebase


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s - %(message)s",
)

logger = logging.getLogger(__name__)


def create_app():
    app = Flask(__name__)

    @app.get("/")
    def index():
        return "3D Room Backend Online"

    @app.get("/health")
    def health():
        return jsonify({
            "status": "ok",
            "service": "3d-room-backend",
        })

    @app.get("/auth/room")
    def auth_room():
        token = request.args.get("token", "")

        if not token:
            body = request.get_json(silent=True) or {}
            token = body.get("token", "")

        if not token:
            return jsonify({"ok": False, "error": "Missing token"}), 400

        try:
            payload = room_service.validate_room_token(token)
        except room_service.ExpiredRoomToken:

            logger.warning("[AUTH] expired room token rejected")
            return jsonify({"ok": False, "error": "Room token has expired."}), 401
        except room_service.InvalidRoomToken:



            logger.warning("[AUTH] invalid room token rejected")
            return jsonify({"ok": False, "error": "Invalid room token."}), 401

        logger.info("[AUTH] room token validated for room %s", payload["chatId"])

        return jsonify({
            "ok": True,
            "data": {
                "telegramId": payload["telegramUserId"],
                "firstName": payload.get("firstName", ""),
                "lastName": payload.get("lastName", ""),
                "username": payload.get("username", ""),
                "displayName": payload.get("displayName", ""),
                "roomId": str(payload["chatId"]),
            },
        })

    @app.after_request
    def add_cors_headers(response):
        response.headers["Access-Control-Allow-Origin"] = "*"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
        response.headers["Access-Control-Allow-Methods"] = "GET,POST,OPTIONS"
        return response

    return app


def main():
    try:
        config.validate()
    except EnvironmentError as exc:
        logging.error("[ERROR] %s", exc)
        raise SystemExit(1)

    try:
        init_firebase()
    except Exception:
        logging.exception("[ERROR] Firebase Admin initialization failed")
        raise SystemExit(1)

    start_bot_in_background()

    app = create_app()

    PORT = int(os.environ.get("PORT", "10000"))

    # Render requires listening on 0.0.0.0.
    # No gunicorn — user explicitly wants `python live.py`.
    app.run(
        host="0.0.0.0",
        port=PORT,
        debug=False,
        use_reloader=False,
        threaded=True,
    )


if __name__ == "__main__":
    main()
