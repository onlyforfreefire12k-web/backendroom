import os


class Config:
    def __init__(self):
        self.TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        self.YOUTUBE_API_KEY = os.environ.get("YOUTUBE_API_KEY", "")
        self.FIREBASE_PROJECT_ID = os.environ.get("FIREBASE_PROJECT_ID", "")
        self.FIREBASE_CLIENT_EMAIL = os.environ.get("FIREBASE_CLIENT_EMAIL", "")

        raw_private_key = os.environ.get("FIREBASE_PRIVATE_KEY", "")
        self.FIREBASE_PRIVATE_KEY = (
            raw_private_key
            .replace("\\n", "\n")
            .strip('"')
            .strip("'")
        )

        self.FIREBASE_DATABASE_URL = os.environ.get("FIREBASE_DATABASE_URL", "").rstrip("/")
        if not self.FIREBASE_DATABASE_URL and self.FIREBASE_PROJECT_ID:
            self.FIREBASE_DATABASE_URL = (
                f"https://{self.FIREBASE_PROJECT_ID}-default-rtdb.firebaseio.com"
            )

        self.FRONTEND_URL = os.environ.get("FRONTEND_URL", "").rstrip("/")
        self.ROOM_TOKEN_SECRET = os.environ.get("ROOM_TOKEN_SECRET", "")

        try:
            self.PORT = int(os.environ.get("PORT", "10000"))
        except ValueError:
            self.PORT = 10000

        try:
            self.ROOM_TOKEN_TTL = int(os.environ.get("ROOM_TOKEN_TTL", "900")))
        except ValueError:
            self.ROOM_TOKEN_TTL = 900

    def validate(self):
        required = [
            ("TELEGRAM_BOT_TOKEN", self.TELEGRAM_BOT_TOKEN),
            ("YOUTUBE_API_KEY", self.YOUTUBE_API_KEY),
            ("FIREBASE_PROJECT_ID", self.FIREBASE_PROJECT_ID),
            ("FIREBASE_CLIENT_EMAIL", self.FIREBASE_CLIENT_EMAIL),
            ("FIREBASE_PRIVATE_KEY", self.FIREBASE_PRIVATE_KEY),
            ("FRONTEND_URL", self.FRONTEND_URL),
            ("ROOM_TOKEN_SECRET", self.ROOM_TOKEN_SECRET),
        ]
        missing = [name for name, value in required if not value]
        if missing:
            raise EnvironmentError(
                "Missing required environment variables: " + ", ".join(missing)
            )
