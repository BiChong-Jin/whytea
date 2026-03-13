from datetime import datetime
from typing import Literal
from pydantic import BaseModel


class ChatComment(BaseModel):
    id: str
    author: str
    message: str
    published_at: datetime
    is_super_chat: bool = False
    super_chat_amount: str | None = None


class AnalysisResult(BaseModel):
    overall_sentiment: Literal["positive", "neutral", "negative", "mixed"]
    mood_score: int  # 0–100
    key_themes: list[str]
    highlights: list[str]
    summary: str
    comment_count: int
    window_start: datetime
    window_end: datetime


class WSMessage(BaseModel):
    type: Literal["comment", "analysis", "status", "error"]
    payload: dict
