# 3D Telegram Music Room — Backend

Production backend that wires an **existing** 3D room frontend to Telegram:

```
Telegram Group
      │
      ├── /room
      │      ↓
      │   backend mints short-lived opaque LAUNCH CODE (roomId stored server-side)
      │      ↓
      │   🎮 JOIN ROOM button — Mini App direct link
      │      https://t.me/<bot>/<MINI_APP_SHORT_NAME>?startapp=<code>
      │      ↓
      │   Telegram launch confirmation → Mini App opens INSIDE Telegram
      │      ↓
      │   frontend reads Telegram.WebApp.initData (signed identity + code)
      │      ↓
      │   POST /api/miniapp/exchange
      │      ↓
      │   HMAC verify + code resolve + getChatMember check
      │      ↓
      │   verified session { roomId, telegramId, displayName, sessionToken }
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
| `bot.py` | Telegram handlers: `/room` (Mini App launch button), `/start` (legacy fallback), `/play`, `/vplay`, display-name logic |
| `config.py` | Env configuration + startup validation (private-key `\n` conversion) |
| `firebase_service.py` | Firebase Admin SDK singleton init + server timestamp helper |
| `tg_auth.py` | Telegram Mini App `initData` HMAC verification + `getChatMember` membership checks |
| `launch_service.py` | Short-lived opaque Mini App launch codes (RTDB, rotated per `/room`) |
| `token_service.py` | Signed short-lived room join/session tokens (HMAC via `itsdangerous`) |
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
| `ROOM_TOKEN_TTL_SECONDS` | optional | Join/session token lifetime in seconds (default `3600`) |
| `MINI_APP_SHORT_NAME` | ✅ for Mini App | Mini App short name from @BotFather `/newapp`; JOIN ROOM links become `https://t.me/<bot>/<MINI_APP_SHORT_NAME>?startapp=<code>` |
| `LAUNCH_CODE_TTL_SECONDS` | optional | Mini App launch-code lifetime (default `1800`) |
| `WEBAPP_INITDATA_MAX_AGE_SECONDS` | optional | Max accepted age of Telegram `initData` (default `86400`) |
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

## 5. Telegram bot setup — INCLUDING THE MINI APP (required)

1. Talk to **@BotFather** → `/newbot` → copy the token → `TELEGRAM_BOT_TOKEN`
2. **Register the Mini App** (this is what makes JOIN ROOM open inside Telegram):
   - Send **`/newapp`** to @BotFather and pick your bot.
   - **Title**: e.g. `3D Music Room`
   - **Description**: anything (can be edited later).
   - **Photo/icon**: upload any image (or `/skip`).
   - **Web App URL**: your frontend's **root** HTTPS URL — e.g. `https://node-static-131e8.wasmer.app/` (NO `/room/...` path; must be HTTPS).
   - **Short name**: e.g. `room` → this becomes the value of `MINI_APP_SHORT_NAME`.
   - BotFather confirms the direct link: `https://t.me/<your_bot>/<short_name>` — this is the Mini App runtime now.
   - Manage later with **`/myapps`** (e.g. *Edit Web App URL*).
3. Optional (recommended): `/setprivacy` → **Disable** so the bot can read `/play` messages in groups.
4. Add the bot to your Telegram group (no admin rights strictly required; admin is fine too).
5. No webhook configuration needed — the bot uses long polling.

## 6. Exact Telegram commands

| Where | Command | Effect |
|---|---|---|
| group | `/room` | Mints a launch code and posts the 🎵 3D MUSIC ROOM card with a **🎮 JOIN ROOM Mini App button** (`t.me/<bot>/<MINI_APP_SHORT_NAME>?startapp=<code>`) |
| private | `/start` | **Audium welcome panel**: artwork + short intro + `🚪 Open Room` (personal Mini App room), `➕ Add to Group`, `📖 Commands` (editable panel with `← Back`), and optional `📢 Channel`/`💬 Support` (shown only when `CHANNEL_URL`/`SUPPORT_URL` are set) |
| private | `/start room_<chatId>` | LEGACY browser fallback (deep link) — verifies membership and returns the signed join URL |
| group | `/play <song name>` | Searches YouTube for a **LYRICS** version, **adds it to the room queue** (`mode="lyrics"`) and posts a rich photo card (real thumbnail/title/duration) |
| group | `/vplay <song name>` | Same queue, normal video (`mode="video"`, card shows `🎬 Video`) |
| group | `/pause` · `/resume` | Pause / resume the current item |
| group | `/skip` | Skips the current item; posts the new NOW PLAYING card |
| group | `/stop` | Stops playback and clears the whole queue (TV goes black) |
| group | `/queue` | Shows *now playing* + numbered upcoming items with requester names |

### Music card UI

`/play` and `/vplay` answer with a **photo card** (real YouTube thumbnail,
bold title, requester, real duration, `🎵 Lyrics Video` or `🎬 Video`) plus:

- `🚪 Join Room` — Mini App direct link for the group the command ran in
  (reuses the room's active launch code, so multiple cards stay valid).
- `⏸ Pause` / `▶️ Resume` · `⏭ Skip` · `📋 Queue` — pure CallbackQuery
  buttons (`room_pause|room_resume|room_skip:<room>:<item>[:c]`,
  `room_queue:<room>`). The handler validates the **chat the card lives in ==
  the encoded room**, the **group membership** of the tapper, and the
  **item id**, so a card can only ever control its own room and a stale card
  can never skip the wrong song. Pause/Resume edit the card in place; Skip
  retires the old card to `⏭ SKIPPED` and posts one fresh NOW PLAYING card.
- QUEUED requests show `🎵 ADDED TO QUEUE + Position in queue: #N` — a new
  request never claims to be "now playing" and never interrupts the current
  song (see *Queue behaviour* above).

The Mini App "natural end" advance also posts the next item's card (and the
`⏹ Queue finished.` note) from the backend via the shared `cards.py` layer.

### Queue behaviour (the important part)

A new request **never interrupts** what is already playing:

```
/play  Song A   (User 1)  -> ▶️ Now playing: Song A
/play  Song B   (User 2)  -> 🎵 Added to queue: Song B   Position: #1
/vplay Song C   (User 3)  -> 🎵 Added to queue: Song C   Position: #2
A ends (or /skip) -> B starts automatically
B ends (or /skip) -> C starts automatically
C ends            -> ⏹ Queue finished. isPlaying=false -> black TV
```

`/play` no longer uses the old black-screen/audio-only mode: the selected
lyrics video plays on the TV (`metadata.playbackMode` is mirrored as
`"video"`, with the precise mode in `metadata.currentMode = "lyrics"`).

**One authoritative queue per room**, stored in Firebase at
`rooms/<roomId>/playback`:

```json
{
  "current": {
    "id": "9f1c…", "videoId": "…", "title": "…", "thumbnail": "…",
    "url": "https://www.youtube.com/watch?v=…", "mode": "lyrics",
    "requestedBy": { "telegramUserId": "123456789", "name": "Ayaan Khan" },
    "addedAt": 1735000000000, "startedAt": 1735000000123, "isPlaying": true
  },
  "queue": [ { "…same shape…" } ],
  "updatedAt": 1735000000123
}
```

`rooms/<roomId>/metadata` keeps **all** of its original fields
(`currentVideoId`, `currentTitle`, `currentThumbnail`, `playbackMode`,
`isPlaying`, `startedAt`, `requestedBy`, `requestedByName`) and gains
`currentMode`, `currentItemId`, `queueLength` — nothing was removed, so the
existing 3D frontend keeps working untouched. `rooms/<roomId>/players` is
never touched by queue operations.

### Advancing the queue (one code path)

`room_service.advance_queue(room_id, expected_item_id=None)` is the single
centralized transition used by **both** `/skip` and natural video
completion. It removes the current item, promotes the first queued item with
a fresh `startedAt`, and — when nothing is left — clears playback and sets
`isPlaying=false` so the TV turns black.

When the frontend's YouTube player fires `ENDED`, it calls:

```
POST /api/rooms/<roomId>/ended   { "sessionToken": "<from miniapp exchange>",
                                   "itemId": "<metadata.currentItemId>" }
```

The write runs inside a Firebase **transaction** and only advances when
`itemId` matches what is actually playing, so ten browsers reporting the same
ending produce exactly **one** transition (the others get
`{"duplicate": true}`). `sessionToken` must belong to that room, so one room
can never advance another room's queue.

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

## 9. Exact JOIN ROOM → Mini App flow (primary) + legacy fallback

### Primary: Telegram Mini App launch (Audium-style, opens INSIDE Telegram)

1. Group member sends `/room`. The bot mints a **cryptographically random
   short launch code** (`token_urlsafe(9)` → 12 chars, `startapp`-safe) and
   stores it server-side in RTDB: `launchCodes/<code> = {roomId, createdBy,
   createdByName, createdAt, expiresAt}`. A new `/room` **rotates** the
   room's code — the previous one is deleted (safely invalidated).
2. The bot posts the 🎮 JOIN ROOM button — an official **Mini App direct
   link**: `https://t.me/<bot>/<MINI_APP_SHORT_NAME>?startapp=<code>`.
   Nothing else is in the URL: no bot token, no credentials, no identity.
3. Tapping it shows Telegram's Mini App launch screen and opens the
   frontend registered in BotFather — **inside Telegram**, not in Chrome.
4. The frontend (`telegram-miniapp.js`) calls `Telegram.WebApp.ready()`,
   reads `Telegram.WebApp.initData` (Telegram-signed identity + the
   `start_param` launch code) and POSTs it to `/api/miniapp/exchange`.
5. The backend: verifies the initData HMAC (secret = `HMAC("WebAppData",
   bot_token)`) → **verified** Telegram identity; resolves + expiry-checks
   the launch code → **roomId = group chat id**; re-checks
   `getChatMember(roomId, userId)` → only real members of that group.
6. The backend returns a signed session (existing `ROOM_TOKEN_SECRET`
   mechanism):

```json
{
  "ok": true,
  "telegramId": 123456789,
  "firstName": "Ayaan",
  "lastName": "Khan",
  "username": "ayaank",
  "displayName": "Ayaan Khan",
  "roomId": "-1001234567890",
  "sessionToken": "SIGNED_SESSION",
  "expiresIn": 3600
}
```

Exchange error codes (mapped to friendly messages by `telegram-miniapp.js`):
`missing_init_data`(400) · `invalid_init_data`/`expired_init_data`/`missing_user`(401) ·
`missing_launch_code`(400) · `invalid_launch_code`(404) · `expired_launch_code`(410) ·
`not_room_member`(403) · `membership_check_failed`(503) · `internal_error`(500).

7. The frontend writes `rooms/<roomId>/players/<telegramId>` with
   `{telegramId, firstName, lastName, username, displayName, online: true,
   joinedAt}` — the character head-name is `displayName`; the user never
   types anything. Everyone from the same group lands in the SAME room
   (roomId == group chat id); other groups' codes resolve to other rooms,
   and `getChatMember` blocks non-members — groups stay isolated.

`get_display_name` priority: `first + last` → `first` → `@username` → Telegram ID.

### Legacy fallback: external browser via deep link (UNCHANGED)

`/start room_<chatId>` (issued when the old button was pressed) still works:
it verifies `getChatMember`, signs a join token (`issuedAt`/`expiresAt`),
returns `https://FRONTEND_URL/room/<chatId>?token=SIGNED_TOKEN`, and the
frontend validates it via `POST /api/token/validate`. The Mini App flow is
preferred; keep the fallback for anyone opening the room outside Telegram.

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
- In a test group with the bot: `/room` → tap **JOIN ROOM** → the Mini App
  opens inside Telegram → the frontend exchanges `initData` and enters the
  room (requires the BotFather Mini App registration from section 5)
- Mini App exchange API:
  `curl -X POST http://localhost:10000/api/miniapp/exchange -H 'Content-Type: application/json' -d '{"initData":"<raw initData>"}'`
- Legacy token API (unchanged):
  `curl -X POST http://localhost:10000/api/token/validate -H 'Content-Type: application/json' -d '{"token":"<SIGNED_TOKEN>"}'`
- `/play Kesariya` → verify `rooms/<chatId>/metadata` in the Firebase console; `/room` also creates `launchCodes/<code>` alongside (outside `rooms/`).

## Optional later commands

`room_service.stop_playback / pause_playback / resume_playback` are fully
implemented. To enable them later, register one-line `CommandHandler`s in
`bot.py` (`/stop` → `stop_playback(chat.id)`, etc.).

## Logging & secrets

Logs use `[ROOM]`, `[PLAY]`, `[JOIN]`, `[AUTH]`, `[ERROR]` tags. Bot token,
YouTube key, Firebase private key and `ROOM_TOKEN_SECRET` are **never** logged
or returned — log lines contain only IDs and error *types*.
