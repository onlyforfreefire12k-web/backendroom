import logging

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

import config


logger = logging.getLogger(__name__)


class YouTubeServiceError(Exception):
    pass


_youtube = None


def _get_youtube_client():
    global _youtube

    if _youtube is None:
        _youtube = build(
            "youtube",
            "v3",
            developerKey=config.YOUTUBE_API_KEY,
            cache_discovery=False,
        )

    return _youtube


def search_youtube(query, max_results=5):
    """Search YouTube and return the first suitable video result."""
    logger.info("[PLAY] YouTube search: %s", query)

    try:
        response = (
            _get_youtube_client()
            .search()
            .list(
                part="snippet",
                q=query,
                type="video",
                maxResults=max_results,
            )
            .execute()
        )
    except HttpError as exc:
        logger.error("[ERROR] YouTube API error: %s", exc.resp.status)
        raise YouTubeServiceError("YouTube API error") from exc
    except Exception as exc:
        logger.error("[ERROR] YouTube search failed")
        raise YouTubeServiceError("YouTube search failed") from exc

    for item in response.get("items", []):
        try:
            video_id = item["id"].get("videoId")
            if not video_id:
                continue

            snippet = item.get("snippet", {})
            thumbnails = snippet.get("thumbnails", {})
            thumbnail = (
                thumbnails.get("high")
                or thumbnails.get("medium")
                or thumbnails.get("default")
                or {}
            ).get("url", "")

            return {
                "videoId": video_id,
                "title": snippet.get("title", "Unknown"),
                "thumbnail": thumbnail,
            }
        except (KeyError, TypeError):
            continue

    return None


def get_video_details(video_id):
    """Optional helper for future commands."""
    try:
        response = (
            _get_youtube_client()
            .videos()
            .list(
                part="snippet,contentDetails",
                id=video_id,
            )
            .execute()
        )
    except Exception as exc:
        logger.error("[ERROR] YouTube video details failed")
        raise YouTubeServiceError("YouTube video details failed") from exc

    items = response.get("items", [])
    if not items:
        return None

    snippet = items[0].get("snippet", {})
    thumbnails = snippet.get("thumbnails", {})
    return {
        "videoId": video_id,
        "title": snippet.get("title", "Unknown"),
        "thumbnail": (
            thumbnails.get("high")
            or thumbnails.get("medium")
            or thumbnails.get("default")
            or {}
        ).get("url", ""),
    }
