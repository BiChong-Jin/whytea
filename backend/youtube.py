import re
from datetime import datetime, timezone
from urllib.parse import urlparse, parse_qs

import httpx

from .models import ChatComment

YOUTUBE_API_BASE = "https://www.googleapis.com/youtube/v3"


class QuotaExceededError(Exception):
    pass


def extract_video_id(url_or_id: str) -> str:
    """Extract video ID from a YouTube URL or return the ID as-is."""
    url_or_id = url_or_id.strip()
    # Already a bare video ID (11 chars, alphanumeric + _ -)
    if re.fullmatch(r"[A-Za-z0-9_\-]{11}", url_or_id):
        return url_or_id
    parsed = urlparse(url_or_id)
    qs = parse_qs(parsed.query)
    if "v" in qs:
        return qs["v"][0]
    # youtu.be/VIDEO_ID
    if parsed.netloc == "youtu.be":
        return parsed.path.lstrip("/")
    raise ValueError(f"Cannot extract video ID from: {url_or_id!r}")


async def resolve_live_chat_id(video_id: str, api_key: str) -> str:
    """Resolve the active live chat ID for a given video ID."""
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{YOUTUBE_API_BASE}/videos",
            params={
                "part": "liveStreamingDetails",
                "id": video_id,
                "key": api_key,
            },
        )
        resp.raise_for_status()
        data = resp.json()

    items = data.get("items", [])
    if not items:
        raise ValueError(f"Video '{video_id}' not found.")

    details = items[0].get("liveStreamingDetails", {})
    live_chat_id = details.get("activeLiveChatId")
    if not live_chat_id:
        raise ValueError(
            f"Video '{video_id}' has no active live chat. "
            "Make sure the stream is currently live."
        )
    return live_chat_id


class YouTubeChatPoller:
    def __init__(self, live_chat_id: str, api_key: str, default_poll_interval_ms: int = 5000):
        self.live_chat_id = live_chat_id
        self.api_key = api_key
        self.next_page_token: str | None = None
        self.poll_interval_ms: int = default_poll_interval_ms

    async def fetch_next_page(self) -> tuple[list[ChatComment], int]:
        """Fetch the next page of live chat messages.

        Returns:
            (comments, suggested_poll_interval_ms)
        """
        params = {
            "liveChatId": self.live_chat_id,
            "part": "snippet,authorDetails",
            "maxResults": 200,
            "key": self.api_key,
        }
        if self.next_page_token:
            params["pageToken"] = self.next_page_token

        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{YOUTUBE_API_BASE}/liveChat/messages", params=params)
            if resp.status_code == 403:
                body = resp.json()
                errors = body.get("error", {}).get("errors", [])
                reason = errors[0].get("reason", "") if errors else ""
                if reason in ("quotaExceeded", "rateLimitExceeded"):
                    raise QuotaExceededError("YouTube API daily quota exceeded. Monitoring paused.")
            resp.raise_for_status()
            data = resp.json()

        self.next_page_token = data.get("nextPageToken")
        self.poll_interval_ms = data.get("pollingIntervalMillis", self.poll_interval_ms)

        comments: list[ChatComment] = []
        for item in data.get("items", []):
            snippet = item.get("snippet", {})
            author = item.get("authorDetails", {})
            msg_type = snippet.get("type", "")

            is_super_chat = msg_type == "superChatEvent"
            super_chat_amount: str | None = None
            if is_super_chat:
                sc = snippet.get("superChatDetails", {})
                super_chat_amount = sc.get("amountDisplayString")

            message_text = (
                snippet.get("displayMessage")
                or snippet.get("textMessageDetails", {}).get("messageText", "")
            )

            published_at_str = snippet.get("publishedAt", "")
            try:
                published_at = datetime.fromisoformat(
                    published_at_str.replace("Z", "+00:00")
                )
            except ValueError:
                published_at = datetime.now(timezone.utc)

            comments.append(
                ChatComment(
                    id=item["id"],
                    author=author.get("displayName", "Unknown"),
                    message=message_text,
                    published_at=published_at,
                    is_super_chat=is_super_chat,
                    super_chat_amount=super_chat_amount,
                )
            )

        return comments, self.poll_interval_ms
