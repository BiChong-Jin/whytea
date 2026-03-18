import re
from datetime import datetime, timezone
from urllib.parse import urlparse, parse_qs

import pytchat

from .models import ChatComment


class QuotaExceededError(Exception):
    pass


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
    """Verify the stream is live by trying to create a pytchat instance."""
    chat = pytchat.create(video_id=video_id)
    if not chat.is_alive():
        raise ValueError(
            f"Video '{video_id}' has no active live chat. "
            "Make sure the stream is currently live."
        )
    chat.terminate()
    return video_id  # pytchat uses video_id directly, no separate chat ID needed


class YouTubeChatPoller:
    def __init__(self, video_id: str, api_key: str = "", default_poll_interval_ms: int = 5000):
        self.video_id = video_id
        self.poll_interval_ms = default_poll_interval_ms
        self._chat = pytchat.create(video_id=video_id)

    def is_alive(self) -> bool:
        return self._chat.is_alive()

    async def fetch_next_page(self) -> tuple[list[ChatComment], int]:
        """Fetch available live chat messages from pytchat buffer."""
        if not self._chat.is_alive():
            raise ValueError("Live chat has ended or is no longer available.")

        comments: list[ChatComment] = []
        for item in self._chat.get().sync_items():
            is_super_chat = item.type == "superChat"
            super_chat_amount: str | None = None
            if is_super_chat:
                super_chat_amount = getattr(item, "amountString", None)

            try:
                published_at = datetime.fromisoformat(
                    item.datetime.replace("Z", "+00:00")
                )
            except (ValueError, AttributeError):
                published_at = datetime.now(timezone.utc)

            comments.append(
                ChatComment(
                    id=item.id,
                    author=item.author.name,
                    message=item.message,
                    published_at=published_at,
                    is_super_chat=is_super_chat,
                    super_chat_amount=super_chat_amount,
                )
            )

        return comments, self.poll_interval_ms

    def terminate(self) -> None:
        self._chat.terminate()
