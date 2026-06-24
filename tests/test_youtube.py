import pytest

from backend.youtube import _parse_message, extract_video_id


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


def test_parse_message_builds_chat_comment():
    item = {
        "id": "msg-1",
        "snippet": {
            "type": "textMessageEvent",
            "hasDisplayContent": True,
            "displayMessage": "gg well played",
            "publishedAt": "2023-11-14T22:13:20Z",
        },
        "authorDetails": {"displayName": "Alice"},
    }
    comment = _parse_message(item)
    assert comment is not None
    assert comment.author == "Alice"
    assert comment.message == "gg well played"
    assert comment.is_super_chat is False


def test_parse_message_returns_none_for_empty_message():
    item = {
        "id": "msg-2",
        "snippet": {"type": "textMessageEvent", "hasDisplayContent": True, "displayMessage": ""},
        "authorDetails": {"displayName": "Bob"},
    }
    assert _parse_message(item) is None


def test_parse_message_returns_none_when_no_display_content():
    item = {
        "id": "msg-2b",
        "snippet": {"type": "textMessageEvent", "hasDisplayContent": False, "displayMessage": "hidden"},
        "authorDetails": {"displayName": "Bob"},
    }
    assert _parse_message(item) is None


def test_parse_message_captures_super_chat_amount():
    item = {
        "id": "msg-3",
        "snippet": {
            "type": "superChatEvent",
            "hasDisplayContent": True,
            "displayMessage": "take my money",
            "superChatDetails": {"amountDisplayString": "$50.00"},
        },
        "authorDetails": {"displayName": "BigSpender"},
    }
    comment = _parse_message(item)
    assert comment is not None
    assert comment.is_super_chat is True
    assert comment.super_chat_amount == "$50.00"


def test_parse_message_returns_none_for_malformed_item():
    assert _parse_message({"snippet": None}) is None
