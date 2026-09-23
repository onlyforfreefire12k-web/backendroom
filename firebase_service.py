import logging

import firebase_admin
from firebase_admin import credentials, db

import config


logger = logging.getLogger(__name__)

_app = None


def init_firebase():
    global _app

    if _app is not None:
        return _app

    try:
        _app = firebase_admin.get_app()
    except ValueError:
        firebase_credentials = credentials.Certificate({
            "type": "service_account",
            "project_id": config.FIREBASE_PROJECT_ID,
            "private_key": config.FIREBASE_PRIVATE_KEY,
            "client_email": config.FIREBASE_CLIENT_EMAIL,
            "token_uri": "https://oauth2.googleapis.com/token",
        })
        _app = firebase_admin.initialize_app(firebase_credentials, {
            "databaseURL": config.FIREBASE_DATABASE_URL,
        })

    return _app


def ref(path=""):
    init_firebase()
    return db.reference(path or "/")
