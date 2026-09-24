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

import requests

import config

log = logging.getLogger("backend.youtube")

_API_BASE = "https://www.googleapis.com/youtube/v3"
_TIMEOUT_SECONDS = 10

_MUSIC_CATEGORY_ID = "10"  # YouTube category "Music"


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


def _pick_best_item(search_items: list[dict], details: dict[str, dict]) -> dict:
    best_item = search_items[0]
    best_key: tuple[int, int] | None = None
    for index, item in enumerate(search_items):
        video_id = (item.get("id") or {}).get("videoId")
        key = (_video_score(details.get(video_id)), -index)  # keep relevance order
        if best_key is None or key > best_key:
            best_key = key
            best_item = item
    return best_item


def _normalize(video_id: str, snippet: dict) -> dict:
    return {
        "videoId": video_id,
        "title": html.unescape((snippet or {}).get("title") or "").strip(),
        "thumbnail": _best_thumbnail(snippet),
    }


def search_youtube(query: str, max_results: int = 5) -> dict | None:
    """
    Search YouTube for a video matching the query.

    Returns {"videoId", "title", "thumbnail"} or None when nothing matched.
    Raises YouTubeServiceError when the API itself failed.
    """
    query = (query or "").strip()
    if not query:
        return None

    log.info("[PLAY] YouTube search: %r", query)

    search_data = _get(
        "search",
        {
            "part": "snippet",
            "q": query,
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

    best = _pick_best_item(items, details)
    result = _normalize(best["id"]["videoId"], best.get("snippet") or {})

    if details.get(result["videoId"], {}).get("snippet", {}).get("thumbnails"):
        result["thumbnail"] = _best_thumbnail(
            details[result["videoId"]]["snippet"]
        )

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
    return _normalize(item["id"], item.get("snippet") or {})
