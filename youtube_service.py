"""
youtube_service.py
------------------
YouTube Data API v3 integration (server-side only).

Used strictly for SEARCH + METADATA. The backend never downloads media,
never extracts audio/video streams and never uses yt-dlp — playback is done
by the official YouTube player in the existing frontend.

YOUTUBE_API_KEY never leaves this service.

Endpoints used:
    GET youtube/v3/search  (part=snippet, type=video, maxResults=5)
    GET youtube/v3/videos  (part=snippet,contentDetails,status)
"""

from __future__ import annotations

import html
import logging
import re

import requests

import config

log = logging.getLogger("backend.youtube")

_API_BASE = "https://www.googleapis.com/youtube/v3"
_TIMEOUT_SECONDS = 10

_MUSIC_CATEGORY_ID = "10"  # YouTube category "Music"

# Title terms that identify a lyrics rendition (used by /play).
_LYRICS_TERMS = (
    "official lyric video",
    "official lyrics video",
    "lyric video",
    "lyrics video",
    "lyrical video",
    "lyrics",
    "lyric",
    "lyrical",
    "with lyrics",
)

# Channel markers that identify an official/artist/label upload.
_OFFICIAL_CHANNEL_TERMS = ("vevo", "official", " - topic", "records", "music")
_OFFICIAL_TITLE_TERMS = ("official", "official video", "official audio")


class YouTubeServiceError(Exception):
    """Raised when the YouTube API is unreachable or returns an error."""


def _get(endpoint: str, params: dict) -> dict:
    """Perform a GET request against the YouTube Data API with error mapping."""
    url = f"{_API_BASE}/{endpoint}"
    try:
        response = requests.get(
            url,
            params={**params, "key": config.YOUTUBE_API_KEY},
            timeout=_TIMEOUT_SECONDS,
        )
    except requests.RequestException as exc:
        log.error("[ERROR] YouTube network error: %s", type(exc).__name__)
        raise YouTubeServiceError("network error") from exc

    if response.status_code != 200:
        # Do NOT leak response bodies / keys — log only the status class.
        log.error("[ERROR] YouTube API HTTP %s on %s", response.status_code, endpoint)
        raise YouTubeServiceError(f"http {response.status_code}")

    try:
        data = response.json()
    except ValueError as exc:
        log.error("[ERROR] YouTube API invalid JSON on %s", endpoint)
        raise YouTubeServiceError("invalid json") from exc

    if "error" in data:
        log.error("[ERROR] YouTube API error payload on %s", endpoint)
        raise YouTubeServiceError("api error")

    return data


def _best_thumbnail(snippet: dict) -> str:
    thumbnails = (snippet or {}).get("thumbnails") or {}
    for quality in ("maxres", "high", "medium", "default"):
        thumb = thumbnails.get(quality)
        if thumb and thumb.get("url"):
            return thumb["url"]
    return ""


def _video_score(detail: dict | None) -> int:
    """
    Score a candidate result so we prefer actual, embeddable music/video
    results over random uploads with the same keywords.
    """
    if not detail:
        return 0
    score = 0
    status = detail.get("status") or {}
    snippet = detail.get("snippet") or {}
    if status.get("embeddable", True):
        score += 100
    if snippet.get("categoryId") == _MUSIC_CATEGORY_ID:
        score += 40
    if snippet.get("liveBroadcastContent", "none") == "none":
        score += 10
    return score


def _lyrics_bonus(search_item: dict, detail: dict | None) -> int:
    """
    Extra score for /play: prefer LYRICS renditions, and prefer official /
    artist / label channels when they are available.
    """
    snippet = (detail or {}).get("snippet") or search_item.get("snippet") or {}
    title = html.unescape(snippet.get("title") or "").lower()
    channel = html.unescape(snippet.get("channelTitle") or "").lower()

    bonus = 0
    if "official lyric video" in title or "official lyrics video" in title:
        bonus += 220
    elif "lyric video" in title or "lyrics video" in title or "lyrical video" in title:
        bonus += 180
    elif any(term in title for term in ("lyrics", "lyric", "lyrical")):
        bonus += 140

    if any(term in channel for term in _OFFICIAL_CHANNEL_TERMS):
        bonus += 60
    if any(term in title for term in _OFFICIAL_TITLE_TERMS):
        bonus += 25

    # Avoid covers/remixes when a straight lyrics upload exists.
    if any(term in title for term in ("cover", "remix", "karaoke", "instrumental")):
        bonus -= 90
    return bonus


def _pick_best_item(
    search_items: list[dict],
    details: dict[str, dict],
    prefer_lyrics: bool = False,
) -> dict:
    best_item = search_items[0]
    best_key: tuple[int, int] | None = None
    for index, item in enumerate(search_items):
        video_id = (item.get("id") or {}).get("videoId")
        detail = details.get(video_id)
        score = _video_score(detail)
        if prefer_lyrics:
            score += _lyrics_bonus(item, detail)
        key = (score, -index)  # relevance order breaks ties
        if best_key is None or key > best_key:
            best_key = key
            best_item = item
    return best_item


_DURATION_PATTERN = re.compile(r"^PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?$")


def parse_duration_seconds(iso_duration: str | None) -> int | None:
    """Parse a YouTube ISO-8601 duration (e.g. PT3M49S) into seconds."""
    if not iso_duration:
        return None
    match = _DURATION_PATTERN.match(iso_duration.strip())
    if not match:
        return None
    hours, minutes, seconds = (int(g) if g else 0 for g in match.groups())
    return hours * 3600 + minutes * 60 + seconds


def format_duration(seconds: int | None) -> str | None:
    """229 -> '3:49', 3723 -> '1:02:03'."""
    if seconds is None or seconds < 0:
        return None
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def attach_duration(result: dict, detail: dict | None) -> None:
    """Fill durationSec/durationText on a normalized result from videos.list."""
    content = (detail or {}).get("contentDetails") or {}
    seconds = parse_duration_seconds(content.get("duration"))
    if seconds is not None:
        result["durationSec"] = seconds
        result["durationText"] = format_duration(seconds)


def _normalize(video_id: str, snippet: dict) -> dict:
    return {
        "videoId": video_id,
        "title": html.unescape((snippet or {}).get("title") or "").strip(),
        "thumbnail": _best_thumbnail(snippet),
        "url": f"https://www.youtube.com/watch?v={video_id}",
    }


def search_youtube(
    query: str, max_results: int = 5, prefer_lyrics: bool = False
) -> dict | None:
    """
    Search YouTube for a video matching the query.

    prefer_lyrics=True (used by /play) biases the search and the ranking
    towards LYRICS versions ("official lyric video", "lyrics video", ...)
    and towards official/artist/label channels.

    Returns {"videoId", "title", "thumbnail", "url"} or None when nothing
    matched. Raises YouTubeServiceError when the API itself failed.
    """
    query = (query or "").strip()
    if not query:
        return None

    search_query = query
    if prefer_lyrics and not any(term in query.lower() for term in ("lyric", "lyrics")):
        search_query = f"{query} lyrics"

    log.info(
        "[PLAY] YouTube search: %r (lyrics=%s)", search_query, bool(prefer_lyrics)
    )

    search_data = _get(
        "search",
        {
            "part": "snippet",
            "q": search_query,
            "type": "video",
            "maxResults": max(1, min(int(max_results), 10)),
            "videoEmbeddable": "true",  # must be playable inside the 3D TV
            "safeSearch": "none",
        },
    )

    items = [
        item
        for item in search_data.get("items", [])
        if (item.get("id") or {}).get("videoId")
    ]
    if not items:
        return None

    details: dict[str, dict] = {}
    try:
        ids = ",".join((item["id"]["videoId"] for item in items))
        details_data = _get(
            "videos",
            {"part": "snippet,contentDetails,status", "id": ids, "maxResults": len(items)},
        )
        details = {d["id"]: d for d in details_data.get("items", []) if d.get("id")}
    except YouTubeServiceError:
        # Search already returned candidates — degrade gracefully.
        details = {}

    best = _pick_best_item(items, details, prefer_lyrics=prefer_lyrics)
    result = _normalize(best["id"]["videoId"], best.get("snippet") or {})

    best_detail = details.get(result["videoId"]) or {}
    detail_snippet = best_detail.get("snippet") or {}
    if detail_snippet.get("thumbnails"):
        result["thumbnail"] = _best_thumbnail(detail_snippet)
    if detail_snippet.get("title"):
        result["title"] = html.unescape(detail_snippet["title"]).strip()
    if detail_snippet.get("channelTitle"):
        result["channelTitle"] = html.unescape(detail_snippet["channelTitle"]).strip()
    attach_duration(result, best_detail)

    log.info("[PLAY] YouTube selected videoId=%s title=%r", result["videoId"], result["title"])
    return result


def get_video_details(video_id: str) -> dict | None:
    """Fetch metadata for one video. Returns None when not found."""
    video_id = (video_id or "").strip()
    if not video_id:
        return None

    data = _get(
        "videos",
        {"part": "snippet,contentDetails,status", "id": video_id, "maxResults": 1},
    )
    items = data.get("items") or []
    if not items:
        return None
    item = items[0]
    result = _normalize(item["id"], item.get("snippet") or {})
    attach_duration(result, item)
    return result
