import asyncio
import json
from datetime import datetime, timezone

import anthropic

from .config import settings
from .models import AnalysisResult, ChatComment

SYSTEM_PROMPT = """\
You are a live audience sentiment analyst for YouTube streams.
You receive batches of live chat comments and return a structured JSON analysis.
Be concise and objective.
Mood score: 0 = extremely negative, 50 = neutral, 100 = extremely positive.
Always return valid JSON matching the requested schema exactly — no markdown fences, no extra keys.\
"""

LANGUAGES = {
    "en": "English",
    "zh": "Chinese (Simplified)",
    "ja": "Japanese",
}


def _format_comments(comments: list[ChatComment]) -> str:
    lines: list[str] = []
    for c in comments:
        prefix = f"[SUPERCHAT {c.super_chat_amount}] " if c.is_super_chat else ""
        lines.append(f"{prefix}[{c.author}]: {c.message}")
    return "\n".join(lines)


class ChatAnalyzer:
    def __init__(self) -> None:
        self._buffer: list[ChatComment] = []
        self._lock = asyncio.Lock()
        self._client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)

    async def add_comments(self, comments: list[ChatComment]) -> None:
        async with self._lock:
            self._buffer.extend(comments)

    async def run_analysis(self, window_start: datetime, window_end: datetime, language: str = "en") -> AnalysisResult | None:
        async with self._lock:
            batch = list(self._buffer[-settings.max_comments_per_batch:])

        if not batch:
            return None

        return await self._call_claude(batch, window_start, window_end, language)

    async def reanalyze(self, window_start: datetime, window_end: datetime, language: str) -> AnalysisResult | None:
        """Re-run analysis on the current buffer with a different language."""
        async with self._lock:
            batch = list(self._buffer[-settings.max_comments_per_batch:])

        if not batch:
            return None

        return await self._call_claude(batch, window_start, window_end, language)

    async def _call_claude(self, batch: list[ChatComment], window_start: datetime, window_end: datetime, language: str = "en") -> AnalysisResult | None:
        lang_name = LANGUAGES.get(language, "English")
        formatted = _format_comments(batch)
        user_prompt = f"""\
Analyze the following {len(batch)} YouTube live chat comments from the last \
{settings.analysis_interval_seconds} seconds.

--- COMMENTS START ---
{formatted}
--- COMMENTS END ---

Respond ENTIRELY in {lang_name} — all field values must be written in {lang_name}.

Return a JSON object with exactly these fields:
{{
  "overall_sentiment": "positive" | "neutral" | "negative" | "mixed",
  "mood_score": <integer 0-100>,
  "key_themes": [<3 to 7 short theme strings in {lang_name}>],
  "highlights": [<3 to 5 verbatim notable quotes, prefer Super Chats>],
  "summary": "<2-4 sentence paragraph describing the audience reaction, written in {lang_name}>"
}}
"""
        try:
            response = await self._client.messages.create(
                model=settings.claude_model,
                max_tokens=1024,
                system=SYSTEM_PROMPT,
                messages=[
                    {"role": "user", "content": user_prompt},
                    {"role": "assistant", "content": "{"},
                ],
            )
            raw = "{" + response.content[0].text.strip()
            data = json.loads(raw)
            return AnalysisResult(
                overall_sentiment=data["overall_sentiment"],
                mood_score=int(data["mood_score"]),
                key_themes=data["key_themes"],
                highlights=data["highlights"],
                summary=data["summary"],
                comment_count=len(batch),
                window_start=window_start,
                window_end=window_end,
            )
        except Exception as exc:
            raise RuntimeError(f"Claude analysis failed: {exc}") from exc
