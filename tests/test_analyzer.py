from datetime import datetime, timezone

import pytest

from backend.analyzer import ChatAnalyzer, _format_comments
from backend.config import settings
from backend.models import ChatComment


def _make_comment(message="hello", author="Alice", is_super_chat=False, super_chat_amount=None):
    return ChatComment(
        id="1",
        author=author,
        message=message,
        published_at=datetime.now(timezone.utc),
        is_super_chat=is_super_chat,
        super_chat_amount=super_chat_amount,
    )


def test_format_comments_plain_message():
    formatted = _format_comments([_make_comment(message="hi there", author="Alice")])
    assert formatted == "[Alice]: hi there"


def test_format_comments_prefixes_super_chats():
    comment = _make_comment(message="take my money", author="Rich", is_super_chat=True, super_chat_amount="$10.00")
    formatted = _format_comments([comment])
    assert formatted == "[SUPERCHAT $10.00] [Rich]: take my money"


@pytest.mark.asyncio
async def test_add_comments_trims_buffer_to_max_size():
    analyzer = ChatAnalyzer()
    max_buffer = settings.max_comments_per_batch * 10

    await analyzer.add_comments([_make_comment(message=str(i)) for i in range(max_buffer + 50)])

    assert len(analyzer._buffer) == max_buffer
    # the oldest comments should have been dropped, newest kept
    assert analyzer._buffer[-1].message == str(max_buffer + 49)


@pytest.mark.asyncio
async def test_run_analysis_returns_none_when_buffer_empty():
    analyzer = ChatAnalyzer()
    window_end = datetime.now(timezone.utc)
    result = await analyzer.run_analysis(window_end, window_end, "en")
    assert result is None


class _FakeMessage:
    def __init__(self, content):
        self.content = content


class _FakeChoice:
    def __init__(self, content):
        self.message = _FakeMessage(content)


class _FakeResponse:
    def __init__(self, content):
        self.choices = [_FakeChoice(content)]


@pytest.mark.asyncio
async def test_call_model_parses_valid_json_response():
    analyzer = ChatAnalyzer()

    async def fake_create(**kwargs):
        return _FakeResponse(
            '"overall_sentiment": "positive", "mood_score": 80, '
            '"key_themes": ["hype"], "highlights": ["lets go"], '
            '"summary": "Chat is excited."}'
        )

    analyzer._client.chat.completions.create = fake_create

    window_end = datetime.now(timezone.utc)
    result = await analyzer._call_model([_make_comment()], window_end, window_end, "en")

    assert result is not None
    assert result.overall_sentiment == "positive"
    assert result.mood_score == 80
    assert result.key_themes == ["hype"]
    assert result.comment_count == 1


@pytest.mark.asyncio
async def test_call_model_wraps_failures_in_runtime_error():
    analyzer = ChatAnalyzer()

    async def fake_create(**kwargs):
        raise ConnectionError("network down")

    analyzer._client.chat.completions.create = fake_create

    window_end = datetime.now(timezone.utc)
    with pytest.raises(RuntimeError, match="Analysis failed"):
        await analyzer._call_model([_make_comment()], window_end, window_end, "en")
