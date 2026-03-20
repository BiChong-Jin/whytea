import asyncio
import json
import re
from datetime import datetime, timezone
from urllib.parse import urlparse, parse_qs

import httpx

from .models import ChatComment


class QuotaExceededError(Exception):
    pass


# YouTube's own web client API key — the same one embedded in youtube.com
_YT_API_KEY = "AIzaSyAO_FJ2SlqU8Q4STEHLGCilw_Y9_11qcW8"
_LIVE_CHAT_ENDPOINT = (
    f"https://www.youtube.com/youtubei/v1/live_chat/get_live_chat?key={_YT_API_KEY}"
)
_CLIENT_VERSION = "2.20260319.01.00"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}


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


async def _fetch_initial_continuation(video_id: str, client: httpx.AsyncClient) -> str:
    """
    Fetch the live chat page and extract the initial continuation token
    from the embedded ytInitialData JSON.
    """
    url = f"https://www.youtube.com/live_chat?v={video_id}&is_from_web=1"
    resp = await client.get(url, headers=_HEADERS)
    resp.raise_for_status()

    match = re.search(r"ytInitialData\s*=\s*", resp.text)
    if not match:
        raise ValueError(
            f"Video '{video_id}' has no active live chat. "
            "Make sure the stream is currently live."
        )

    # Use the JSON decoder to reliably find the end of the object
    # rather than trying to regex-match the whole thing
    try:
        data, _ = json.JSONDecoder().raw_decode(resp.text, match.end())
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"Failed to parse YouTube page data for '{video_id}'"
        ) from exc

    try:
        continuations = data["contents"]["liveChatRenderer"]["continuations"]
    except KeyError:
        raise ValueError(
            f"Video '{video_id}' has no active live chat. "
            "Make sure the stream is currently live."
        )

    for cont in continuations:
        for key in ("timedContinuationData", "invalidationContinuationData", "reloadContinuationData"):
            if key in cont and "continuation" in cont[key]:
                return cont[key]["continuation"]

    raise ValueError(
        f"Video '{video_id}' has no active live chat. "
        "Make sure the stream is currently live."
    )


async def resolve_live_chat_id(video_id: str, api_key: str) -> str:
    """Verify the stream is live by fetching the initial continuation token."""
    async with httpx.AsyncClient(timeout=15.0) as client:
        await _fetch_initial_continuation(video_id, client)
    return video_id


def _build_payload(continuation: str, visitor_data: str) -> dict:
    return {
        "context": {
            "client": {
                "clientName": "WEB",
                "clientVersion": _CLIENT_VERSION,
                "visitorData": visitor_data,
            }
        },
        "continuation": continuation,
    }


def _parse_message(renderer: dict, *, is_super_chat: bool) -> ChatComment | None:
    try:
        message = "".join(
            run.get("text", "")
            for run in renderer.get("message", {}).get("runs", [])
        )
        author = renderer.get("authorName", {}).get("simpleText", "Unknown")
        msg_id = renderer.get("id", "")
        ts_usec = renderer.get("timestampUsec")
        published_at = (
            datetime.fromtimestamp(int(ts_usec) / 1_000_000, tz=timezone.utc)
            if ts_usec
            else datetime.now(timezone.utc)
        )
        super_chat_amount = (
            renderer.get("purchaseAmountText", {}).get("simpleText")
            if is_super_chat
            else None
        )
        return ChatComment(
            id=msg_id,
            author=author,
            message=message,
            published_at=published_at,
            is_super_chat=is_super_chat,
            super_chat_amount=super_chat_amount,
        )
    except Exception:
        return None


class YouTubeChatPoller:
    def __init__(self, video_id: str, api_key: str = "", default_poll_interval_ms: int = 5000):
        self.video_id = video_id
        self.poll_interval_ms = default_poll_interval_ms
        self._continuation: str | None = None
        self._visitor_data: str = ""
        self._alive = True
        self._client = httpx.AsyncClient(timeout=15.0, headers=_HEADERS)

    def is_alive(self) -> bool:
        return self._alive

    async def fetch_next_page(self) -> tuple[list[ChatComment], int]:
        if not self._alive:
            raise ValueError("Live chat has ended or is no longer available.")

        # Lazily initialise the continuation token on the first call
        if self._continuation is None:
            self._continuation = await _fetch_initial_continuation(self.video_id, self._client)

        resp = await self._client.post(
            _LIVE_CHAT_ENDPOINT,
            json=_build_payload(self._continuation, self._visitor_data),
        )
        resp.raise_for_status()
        data = resp.json()

        # Update visitor data if YouTube sends one back (improves session continuity)
        new_visitor = data.get("responseContext", {}).get("visitorData", "")
        if new_visitor:
            self._visitor_data = new_visitor

        chat_cont = data.get("continuationContents", {}).get("liveChatContinuation", {})
        if not chat_cont:
            self._alive = False
            raise ValueError("Live chat has ended or is no longer available.")

        # Extract next continuation token and suggested poll interval
        timeout_ms = self.poll_interval_ms
        for cont in chat_cont.get("continuations", []):
            for key in ("timedContinuationData", "invalidationContinuationData"):
                if key in cont:
                    self._continuation = cont[key].get("continuation", self._continuation)
                    timeout_ms = cont[key].get("timeoutMs", timeout_ms)
                    break

        self.poll_interval_ms = timeout_ms

        # Parse chat messages
        comments: list[ChatComment] = []
        for action in chat_cont.get("actions", []):
            item = action.get("addChatItemAction", {}).get("item", {})
            if "liveChatTextMessageRenderer" in item:
                c = _parse_message(item["liveChatTextMessageRenderer"], is_super_chat=False)
            elif "liveChatPaidMessageRenderer" in item:
                c = _parse_message(item["liveChatPaidMessageRenderer"], is_super_chat=True)
            else:
                continue
            if c:
                comments.append(c)

        return comments, timeout_ms

    def terminate(self) -> None:
        self._alive = False
        # Schedule the async close without blocking — event loop is always running here
        asyncio.ensure_future(self._client.aclose())
