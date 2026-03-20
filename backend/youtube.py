"""
YouTube live chat poller — fully async, no pytchat threads.

Continuation token generation is ported from pytchat's liveparam module:
the token is a base64-encoded protobuf built from the video ID, channel ID,
and a set of current timestamps. This avoids scraping ytInitialData from
the live chat page (which YouTube often omits for headless requests).
"""

import asyncio
import json
import logging
import random
import re
import time
from base64 import urlsafe_b64encode
from datetime import datetime, timezone
from urllib.parse import quote, urlparse, parse_qs

import httpx

from .models import ChatComment

logger = logging.getLogger(__name__)

# YouTube's internal web-client API key (embedded in youtube.com itself)
_YT_API_KEY = "AIzaSyAO_FJ2SlqU8Q4STEHLGCilw_Y9_11qcW8"
_LIVE_CHAT_ENDPOINT = (
    f"https://www.youtube.com/youtubei/v1/live_chat/get_live_chat?key={_YT_API_KEY}"
)
_CLIENT_VERSION = "2.20260319.01.00"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36 Edg/120.0.0.0"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

_MOBILE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Linux; Android 6.0; Nexus 5 Build/MRA58N) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/91.0.4472.114 Mobile Safari/537.36"
    ),
}


class QuotaExceededError(Exception):
    pass


# ── Protobuf helpers (ported from pytchat/paramgen/enc.py) ───────────────────

def _vn(val: int) -> bytes:
    """Encode a protobuf varint."""
    buf = b""
    while val >> 7:
        buf += (val & 0xFF | 0x80).to_bytes(1, "big")
        val >>= 7
    return buf + val.to_bytes(1, "big")


def _rs(field: int, value: str | bytes) -> bytes:
    """Encode a protobuf length-delimited (string/bytes) field."""
    if isinstance(value, str):
        value = value.encode()
    tag = _vn((field << 3) | 2)
    return tag + _vn(len(value)) + value


def _nm(field: int, value: int) -> bytes:
    """Encode a protobuf varint field."""
    return _vn((field << 3) | 0) + _vn(value)


# ── Continuation token generation (ported from pytchat/paramgen/liveparam.py) ─

def _make_header(video_id: str, channel_id: str) -> bytes:
    s1 = _rs(3, _rs(1, video_id)) + _rs(5, _rs(1, channel_id) + _rs(2, video_id))
    s3 = _rs(48687757, _rs(1, video_id))
    return urlsafe_b64encode(_rs(1, s1) + _rs(3, s3) + _nm(4, 1))


def _build_continuation(video_id: str, channel_id: str) -> str:
    """Build a base64-encoded protobuf continuation token from scratch."""
    n = int(time.time())
    ts1 = int((n - random.uniform(0, 3))           * 1_000_000)
    ts2 = int((n - random.uniform(0.01, 0.99))     * 1_000_000)
    ts3 = int((n - 3 + random.uniform(0, 1))       * 1_000_000)
    ts4 = int((n - random.uniform(600, 3600))      * 1_000_000)
    ts5 = int((n - random.uniform(0.01, 0.99))     * 1_000_000)

    body = _rs(9, b"".join([
        _nm(1, 0), _nm(2, 0), _nm(3, 0), _nm(4, 0),
        _rs(7, ""), _nm(8, 0), _rs(9, ""),
        _nm(10, ts2), _nm(11, 3), _nm(15, 0),
    ]))

    entity = b"".join([
        _rs(3, _make_header(video_id, channel_id)),
        _nm(5, ts1), _nm(6, 0), _nm(7, 0), _nm(8, 1),
        body,
        _nm(10, ts3), _nm(11, ts4), _nm(13, 1),
        _rs(16, _nm(1, 1)),
        _nm(17, 0), _rs(19, _nm(1, 0)), _nm(20, ts5),
    ])

    return quote(urlsafe_b64encode(_rs(119693434, entity)).decode())


# ── Channel ID fetching ───────────────────────────────────────────────────────

async def _fetch_channel_id(video_id: str, client: httpx.AsyncClient) -> str:
    """
    Fetch the channel ID for a video — required to build the continuation token.
    Tries the embed page first (desktop), falls back to the mobile watch page.
    """
    resp = await client.get(
        f"https://www.youtube.com/embed/{video_id}", headers=_HEADERS
    )
    resp.raise_for_status()

    # The embed page serialises channel IDs with escaped quotes
    match = re.search(r'\\"channelId\\":\\"(.{24})\\"', resp.text)
    if match:
        return match.group(1)

    # Fallback: mobile watch page uses unescaped JSON
    resp2 = await client.get(
        f"https://m.youtube.com/watch?v={video_id}", headers=_MOBILE_HEADERS
    )
    match2 = re.search(r'"channelId"\s*:\s*"(.{24})"', resp2.text)
    if match2:
        return match2.group(1)

    raise ValueError(
        f"Cannot find channel ID for '{video_id}'. "
        "Check that the video ID is correct and the stream is public."
    )


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
    Verify the stream is live by making one real chat request.
    Raises ValueError if the stream is not live or has no chat.
    """
    async with httpx.AsyncClient(timeout=15.0) as client:
        channel_id = await _fetch_channel_id(video_id, client)
        continuation = _build_continuation(video_id, channel_id)

        resp = await client.post(
            _LIVE_CHAT_ENDPOINT,
            json={
                "context": {"client": {"clientName": "WEB", "clientVersion": _CLIENT_VERSION}},
                "continuation": continuation,
            },
            headers=_HEADERS,
        )
        resp.raise_for_status()
        data = resp.json()

    if not data.get("continuationContents", {}).get("liveChatContinuation"):
        raise ValueError(
            f"Video '{video_id}' has no active live chat. "
            "Make sure the stream is currently live."
        )

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


def _run_to_text(run: dict) -> str:
    """Convert a single message run to a plain-text string.

    YouTube encodes messages as a list of runs — either plain text or emoji.
    Standard emojis carry the Unicode character in emojiId; custom channel
    emojis carry only an image URL, so we fall back to their shortcut text.
    """
    if "text" in run:
        return run["text"]
    emoji = run.get("emoji", {})
    emoji_id = emoji.get("emojiId", "")
    # Standard Unicode emoji: emojiId is the actual character (e.g. "😂")
    if emoji_id and not emoji.get("isCustomEmoji"):
        return emoji_id
    # Custom channel emoji: use the shortcut (e.g. ":channel-name-emoji:")
    shortcuts = emoji.get("shortcuts")
    if shortcuts:
        return shortcuts[0]
    # Last resort: use the accessibility label
    label = emoji.get("image", {}).get("accessibility", {}).get("accessibilityData", {}).get("label", "")
    return f":{label}:" if label else ""


def _parse_message(renderer: dict, *, is_super_chat: bool) -> ChatComment | None:
    try:
        message = "".join(
            _run_to_text(run)
            for run in renderer.get("message", {}).get("runs", [])
        ).strip()
        if not message:
            return None
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
        self._channel_id: str | None = None
        self._visitor_data: str = ""
        self._alive = True
        self._client = httpx.AsyncClient(timeout=15.0, headers=_HEADERS)

    def is_alive(self) -> bool:
        return self._alive

    async def fetch_next_page(self) -> tuple[list[ChatComment], int]:
        if not self._alive:
            raise ValueError("Live chat has ended or is no longer available.")

        # Lazily build the first continuation token on the first call
        if self._continuation is None:
            if self._channel_id is None:
                self._channel_id = await _fetch_channel_id(self.video_id, self._client)
            self._continuation = _build_continuation(self.video_id, self._channel_id)

        resp = await self._client.post(
            _LIVE_CHAT_ENDPOINT,
            json=_build_payload(self._continuation, self._visitor_data),
        )
        resp.raise_for_status()
        data = resp.json()

        # Persist visitor data for session continuity
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
        asyncio.ensure_future(self._client.aclose())
