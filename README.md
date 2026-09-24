# 3D Telegram Music Room — Backend

Production backend that wires an **existing** 3D room frontend to Telegram:

```
Telegram Group
      │
      ├── /room
      │      ↓
      │   JOIN ROOM button  (deep link → /start room_<chatId>)
      │      ↓
      │   membership verified (getChatMember)
      │      ↓
      │   signed short-lived room token
      │      ↓
      │   https://FRONTEND/room/<chatId>?token=SIGNED_TOKEN
      │      ↓
      │   frontend POSTs token → /api/token/validate → verified identity
      │
      ├── /play song name
      │      ↓
      │   YouTube Data API v3 (search + metadata ONLY)
      │      ↓
      │   Firebase  rooms/<chatId>/metadata  (playbackMode="audio")
      │      ↓
      │   every connected browser updates the 3D room (music theme + audio)
      │
      └── /vplay song name
             ↓
          YouTube Data API v3
             ↓
          Firebase  rooms/<chatId>/metadata  (playbackMode="video")
             ↓
          every connected browser shows the actual video on the 3D TV
```

The bot never talks to browsers directly — **Firebase is the sync bus**.

---

## Files

| File | Responsibility |
|---|---|
| `live.py` | Entry point (`python live.py`): Flask health/API server (thread) + Telegram polling (main thread) |
| `bot.py` | Telegram handlers: `/room`, `/start room_<chatId>`, `/play`, `/vplay`, display-name logic |
| `config.py` | Env configuration + startup validation (private-key `\n` conversion) |
| `firebase_service.py` | Firebase Admin SDK singleton init + server timestamp helper |
| `token_service.py` | Signed short-lived room join tokens (HMAC via `itsdangerous`) |
| `youtube_service.py` | YouTube Data API v3 search/metadata (REST via `requests`, key stays server-side) |
| `room_service.py` | All Firebase room reads/writes: playback state, pause/resume/stop helpers |
| `requirements.txt` | Pinned dependencies (no gunicorn) |

---

## 1. Render environment variables

Set these in the Render dashboard (**Environment** tab):

| Variable | Required | Notes |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | ✅ | From @BotFather |
| `YOUTUBE_API_KEY` | ✅ | YouTube Data API v3 key |
| `FIREBASE_PROJECT_ID` | ✅ | Service-account JSON `project_id` |
| `FIREBASE_CLIENT_EMAIL` | ✅ | Service-account JSON `client_email` |
| `FIREBASE_PRIVATE_KEY` | ✅ | Service-account JSON `private_key`; paste with literal `\n`s — the backend runs `private_key.replace("\\n","\n")` |
| `FRONTEND_URL` | ✅ | Origin of the existing 3D frontend, no trailing slash, e.g. `https://my-room.vercel.app` |
| `ROOM_TOKEN_SECRET` | ✅ | Long random string (Render can auto-generate). Signs join tokens. |
| `PORT` | auto | Render injects it; default `10000` |
| `FIREBASE_DATABASE_URL` | ✅ | Exact RTDB URL from the Firebase console — region-specific, e.g. `https://room-131c2-default-rtdb.asia-southeast1.firebasedatabase.app`. Never guessed from the project id. |
| `ROOM_TOKEN_TTL_SECONDS` | optional | Join-token lifetime in seconds (default `3600`) |
| `PYTHON_VERSION` | optional | Pin e.g. `3.12.8` |

## 2. Render build/start configuration

- **New → Web Service**, Runtime: **Python 3**
- If the repo also contains other code, set **Root Directory** to `backend`
- **Build Command:** `pip install -r requirements.txt`
- **Start Command:** `python live.py`
- **Health Check Path:** `/health`
- No gunicorn/uvicorn. Flask binds `0.0.0.0:$PORT` in a thread while the bot long-polls on the main thread.
- Alternatively deploy with the included `render.yaml` blueprint.

## 3. Firebase Admin setup

1. Firebase Console → your project → **Project settings → Service accounts**
2. **Generate new private key** (JSON). From it copy `project_id`, `client_email`, `private_key` into the env vars above.
3. Enable **Realtime Database** (not Firestore), then copy its **exact URL** from the Realtime Database tab into `FIREBASE_DATABASE_URL` — RTDB URLs are region-specific (e.g. `https://room-131c2-default-rtdb.asia-southeast1.firebasedatabase.app`) and are never derived from the project id.
4. Recommended security rules for this architecture (browsers are unauthenticated; only the Admin SDK may write `metadata` — the Admin SDK bypasses rules so the bot always works):

```json
{
  "rules": {
    "rooms": {
      "$roomId": {
        ".read": true,
        ".write": false,
        "players": {
          "$playerId": {
            ".write": true
          }
        }
      }
    }
  }
}
```

This lets frontend clients read everything and write only player presence nodes (`rooms/<id>/players/<telegramId>`), while `metadata` — and therefore room creation and playback control — is exclusively owned by the bot. Arbitrary room creation through the backend HTTP API is not possible (there is no such endpoint).

## 4. YouTube API setup

1. Google Cloud Console → create/select a project
2. **APIs & Services → Library → YouTube Data API v3 → Enable**
3. **Credentials → Create API key** (restrict it to the YouTube Data API v3)
4. The key is used **only server-side** (`youtube_service.py`) for `search.list` and `videos.list`. The backend never downloads media, never uses yt-dlp, never extracts stream URLs — playback uses the official YouTube player in the frontend.

## 5. Telegram bot setup

1. Talk to **@BotFather** → `/newbot` → copy the token → `TELEGRAM_BOT_TOKEN`
2. Optional (recommended): `/setprivacy` → **Disable** so the bot can read `/play` messages in groups (commands are always visible; privacy off lets it see everything)
3. Add the bot to your Telegram group (no admin rights strictly required; admin is fine too)
4. No webhook configuration needed — the bot uses long polling.

## 6. Exact Telegram commands

| Where | Command | Effect |
|---|---|---|
| group | `/room` | Posts the 🎵 3D MUSIC ROOM card with a **🎮 JOIN ROOM** button |
| private | `/start room_<chatId>` | (via the button) verifies membership and returns the signed join URL |
| group | `/play <song name>` | Searches YouTube, sets `playbackMode="audio"` |
| group | `/vplay <song name>` | Searches YouTube, sets `playbackMode="video"` |

Validation replies:

- `/play` with no query → `Please provide a song name.` + example
- `/vplay` with no query → `Please provide a video name.` + example
- No result → `❌ No matching YouTube result found.`
- API failure → `❌ YouTube search is temporarily unavailable.`

Success replies keep the clean format:

```
🎵 Now playing

Song Title

Mode: Music

Requested by: @username
```
```
📺 Now playing video

Video Title

Mode: Video

Requested by: @username
```

## 7. Exact Firebase data written by `/play`

`rooms/<telegramChatId>/metadata` (full overwrite):

```json
{
  "currentVideoId": "kJQP7kiw5Fk",
  "currentTitle": "Kesariya (Song Title From YouTube)",
  "currentThumbnail": "https://i.ytimg.com/vi/<id>/hqdefault.jpg",
  "playbackMode": "audio",
  "isPlaying": true,
  "requestedBy": 123456789,
  "requestedByName": "Ayaan Khan",
  "startedAt": "<firebase server timestamp ms>"
}
```

`playbackMode="audio"` ⇒ frontend keeps its **music theme screen** (🎵 NOW PLAYING + title) while the YouTube player supplies audio — the video itself is not shown.

## 8. Exact Firebase data written by `/vplay`

Identical shape, with `"playbackMode": "video"` ⇒ the frontend renders the actual YouTube video on the 3D TV.

`startedAt` is a **Firebase server timestamp** so every browser computes
`elapsed = now - startedAt` and late joiners start at the current position —
never from 0. New users never trigger a new song; browsers read the existing
`metadata` node on join.

## 9. Exact JOIN ROOM URL/token flow

Telegram URL buttons are static (they can't carry per-user data), so the
secure flow is a 2-tap deep link:

1. Group member taps **🎮 JOIN ROOM** → opens `https://t.me/<bot>?start=room_<chatId>` → presses START.
2. Bot receives `/start room_<chatId>` **with the verified Telegram identity of the tapper**.
3. Bot checks `getChatMember(chatId, userId)` — non-members are rejected (prevents forged room access).
4. Bot signs a token (HMAC-SHA256, TTL default 1h):

```json
{
  "telegramUserId": 123456789,
  "chatId": "-1001234567890",
  "firstName": "Ayaan",
  "lastName": "Khan",
  "username": "ayaank",
  "displayName": "Ayaan Khan",
  "issuedAt": 1735000000,
  "expiresAt": 1735003600
}
```

5. Bot replies with a URL button:
   `https://FRONTEND_URL/room/-1001234567890?token=SIGNED_TOKEN`
6. Frontend extracts `token` and calls `POST /api/token/validate` with JSON `{"token": "..."}`.
7. Backend verifies signature + expiry and returns:

```json
{
  "ok": true,
  "telegramId": 123456789,
  "firstName": "Ayaan",
  "lastName": "Khan",
  "username": "ayaank",
  "displayName": "Ayaan Khan",
  "roomId": "-1001234567890"
}
```

(`401 {"ok":false,"error":"invalid_token"|"expired_token"}` on failure.)

8. Frontend writes `rooms/<roomId>/players/<telegramId>` with `{telegramId, firstName, lastName, username, displayName, online: true, joinedAt}` — the character head-name is always `displayName`; the user never types anything.

`get_display_name` priority: `first + last` → `first` → `@username` → Telegram ID.

## 10. Local testing

```bash
cd backend
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# create local env
cp .env.example .env   # fill in real values
export $(grep -v '^#' .env | xargs)   # or use direnv/python-dotenv

# run (long polling — no public URL or ngrok needed for Telegram)
python live.py
```

Checks:

- `curl http://localhost:10000/` → `3D Room Backend Online`
- `curl http://localhost:10000/health` → `{"status":"ok","service":"3d-room-backend"}`
- In a test group with the bot: `/room` → JOIN ROOM → START → signed link
  (for local use set `FRONTEND_URL=http://localhost:3000` so the token-debug
  call hits your local frontend origin)
- `/play Kesariya` → verify `rooms/<chatId>/metadata` in the Firebase console
- Token API: `curl -X POST http://localhost:10000/api/token/validate -H 'Content-Type: application/json' -d '{"token":"<SIGNED_TOKEN>"}'`

## Optional later commands

`room_service.stop_playback / pause_playback / resume_playback` are fully
implemented. To enable them later, register one-line `CommandHandler`s in
`bot.py` (`/stop` → `stop_playback(chat.id)`, etc.).

## Logging & secrets

Logs use `[ROOM]`, `[PLAY]`, `[JOIN]`, `[AUTH]`, `[ERROR]` tags. Bot token,
YouTube key, Firebase private key and `ROOM_TOKEN_SECRET` are **never** logged
or returned — log lines contain only IDs and error *types*.
