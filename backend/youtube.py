"""YouTube live chat poller using the official YouTube Data API v3."""

import asyncio
import logging
import re
from datetime import datetime, timezone
from urllib.parse import urlparse, parse_qs

import httpx

from .models import ChatComment

logger = logging.getLogger(__name__)

_API_BASE = "https://www.googleapis.com/youtube/v3"
_QUOTA_REASONS = {"quotaExceeded", "dailyLimitExceeded"}


class QuotaExceededError(Exception):
    pass


def _error_reasons(resp: httpx.Response) -> set[str]:
    try:
        body = resp.json()
    except ValueError:
        return set()
    return {e.get("reason", "") for e in body.get("error", {}).get("errors", [])}


# ── Public API ────────────────────────────────────────────────────────────────

def extract_video_id(url_or_id: str) -> str:
    """Extract video ID from a YouTube URL or return the ID as-is."""
    url_or_id = url_or_id.strip()
    if re.fullmatch(r"[A-Za-z0-9_\-]{11}", url_or_id):
        return url_or_id
    parsed = urlparse(url_or_id)
    qs = parse_qs(parsed.query)
    if "v" in qs:
        return qs["v"][0]
    if parsed.netloc == "youtu.be":
        return parsed.path.lstrip("/")
    raise ValueError(f"Cannot extract video ID from: {url_or_id!r}")


async def resolve_live_chat_id(video_id: str, api_key: str) -> str:
    """
    Look up the active live chat ID for a video via the YouTube Data API.
    Raises ValueError if the video doesn't exist or isn't currently live,
    and QuotaExceededError if the API key's daily quota is exhausted.
    """
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(
            f"{_API_BASE}/videos",
            params={"part": "liveStreamingDetails", "id": video_id, "key": api_key},
        )

    if resp.status_code == 403 and _error_reasons(resp) & _QUOTA_REASONS:
        raise QuotaExceededError(
            "YouTube API quota exceeded. Try again after the daily reset (midnight Pacific time)."
        )
    resp.raise_for_status()

    items = resp.json().get("items", [])
    if not items:
        raise ValueError(f"Video '{video_id}' was not found or is private.")

    live_chat_id = items[0].get("liveStreamingDetails", {}).get("activeLiveChatId")
    if not live_chat_id:
        raise ValueError(
            f"Video '{video_id}' has no active live chat. "
            "Make sure the stream is currently live."
        )
    return live_chat_id


def _parse_message(item: dict) -> ChatComment | None:
    try:
        snippet = item.get("snippet", {})
        if not snippet.get("hasDisplayContent", True):
            return None

        message = (snippet.get("displayMessage") or "").strip()
        if not message:
            return None

        is_super_chat = snippet.get("type") == "superChatEvent"
        super_chat_amount = (
            snippet.get("superChatDetails", {}).get("amountDisplayString")
            if is_super_chat
            else None
        )

        published_raw = snippet.get("publishedAt")
        published_at = (
            datetime.fromisoformat(published_raw.replace("Z", "+00:00"))
            if published_raw
            else datetime.now(timezone.utc)
        )

        return ChatComment(
            id=item.get("id", ""),
            author=item.get("authorDetails", {}).get("displayName", "Unknown"),
            message=message,
            published_at=published_at,
            is_super_chat=is_super_chat,
            super_chat_amount=super_chat_amount,
        )
    except Exception:
        return None


class YouTubeChatPoller:
    def __init__(self, live_chat_id: str, api_key: str, default_poll_interval_ms: int = 5000):
        self.live_chat_id = live_chat_id
        self.api_key = api_key
        self.poll_interval_ms = default_poll_interval_ms
        self._page_token: str | None = None
        self._alive = True
        self._client = httpx.AsyncClient(timeout=15.0)

    def is_alive(self) -> bool:
        return self._alive

    async def fetch_next_page(self) -> tuple[list[ChatComment], int]:
        if not self._alive:
            raise ValueError("Live chat has ended or is no longer available.")

        params = {
            "liveChatId": self.live_chat_id,
            "part": "snippet,authorDetails",
            "key": self.api_key,
        }
        if self._page_token:
            params["pageToken"] = self._page_token

        resp = await self._client.get(f"{_API_BASE}/liveChat/messages", params=params)

        if resp.status_code == 403:
            reasons = _error_reasons(resp)
            if reasons & _QUOTA_REASONS:
                raise QuotaExceededError(
                    "YouTube API quota exceeded. Polling stopped until the daily reset (midnight Pacific time)."
                )
            if "liveChatEnded" in reasons:
                self._alive = False
                raise ValueError("Live chat has ended or is no longer available.")
        resp.raise_for_status()
        data = resp.json()

        self._page_token = data.get("nextPageToken")
        self.poll_interval_ms = data.get("pollingIntervalMillis", self.poll_interval_ms)

        comments = [
            c for item in data.get("items", [])
            if (c := _parse_message(item)) is not None
        ]
        return comments, self.poll_interval_ms

    def terminate(self) -> None:
        self._alive = False
        asyncio.ensure_future(self._client.aclose())
