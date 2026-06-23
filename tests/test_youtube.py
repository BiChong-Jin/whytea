import pytest

from backend.youtube import _parse_message, _run_to_text, extract_video_id


@pytest.mark.parametrize(
    "value",
    [
        "dQw4w9WgXcQ",
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=30s",
        "https://youtu.be/dQw4w9WgXcQ",
    ],
)
def test_extract_video_id_accepts_known_formats(value):
    assert extract_video_id(value) == "dQw4w9WgXcQ"


def test_extract_video_id_rejects_unparseable_input():
    with pytest.raises(ValueError):
        extract_video_id("not a youtube url at all")


def test_run_to_text_plain_text_run():
    assert _run_to_text({"text": "hello world"}) == "hello world"


def test_run_to_text_standard_unicode_emoji():
    run = {"emoji": {"emojiId": "\U0001F602", "isCustomEmoji": False}}
    assert _run_to_text(run) == "\U0001F602"


def test_run_to_text_custom_emoji_uses_shortcut():
    run = {
        "emoji": {
            "emojiId": "custom-id",
            "isCustomEmoji": True,
            "shortcuts": [":channel-emote:"],
        }
    }
    assert _run_to_text(run) == ":channel-emote:"


def test_run_to_text_custom_emoji_falls_back_to_accessibility_label():
    run = {
        "emoji": {
            "emojiId": "custom-id",
            "isCustomEmoji": True,
            "shortcuts": [],
            "image": {"accessibility": {"accessibilityData": {"label": "PogChamp"}}},
        }
    }
    assert _run_to_text(run) == ":PogChamp:"


def test_parse_message_builds_chat_comment():
    renderer = {
        "id": "msg-1",
        "authorName": {"simpleText": "Alice"},
        "message": {"runs": [{"text": "gg "}, {"text": "well played"}]},
        "timestampUsec": "1700000000000000",
    }
    comment = _parse_message(renderer, is_super_chat=False)
    assert comment is not None
    assert comment.author == "Alice"
    assert comment.message == "gg well played"
    assert comment.is_super_chat is False


def test_parse_message_returns_none_for_empty_message():
    renderer = {
        "id": "msg-2",
        "authorName": {"simpleText": "Bob"},
        "message": {"runs": []},
    }
    assert _parse_message(renderer, is_super_chat=False) is None


def test_parse_message_captures_super_chat_amount():
    renderer = {
        "id": "msg-3",
        "authorName": {"simpleText": "BigSpender"},
        "message": {"runs": [{"text": "take my money"}]},
        "purchaseAmountText": {"simpleText": "$50.00"},
    }
    comment = _parse_message(renderer, is_super_chat=True)
    assert comment is not None
    assert comment.is_super_chat is True
    assert comment.super_chat_amount == "$50.00"
