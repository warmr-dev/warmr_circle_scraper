from circle_leads.scraper.normalize import (
    normalize_chat_message,
    normalize_comment,
    normalize_post,
    parse_timestamp,
    redact_pii,
    strip_html,
)


def test_strip_html_and_entities():
    assert strip_html("<p>We&#39;re <b>hiring</b>!</p>") == "We're hiring !"
    assert strip_html(None) == ""


def test_pii_is_redacted_when_excluded():
    text = "Email me at dana@example.com or call 555-123-4567"
    out = redact_pii(text, ["email_addresses", "phone_numbers"])
    assert "dana@example.com" not in out
    assert "555-123-4567" not in out


def test_pii_is_kept_when_not_excluded():
    text = "Email me at dana@example.com"
    assert "dana@example.com" in redact_pii(text, [])


def test_normalize_post_combines_title_and_body():
    record = normalize_post(
        {"id": 5, "name": "Hiring", "body": {"body": "<p>Need a dev</p>"},
         "url": "/c/x/1", "published_at": "2026-08-30T10:00:00Z",
         "user": {"id": 9, "name": "Dana"}},
        community_url="https://acme.circle.so",
    )
    assert record["content"] == "Hiring\n\nNeed a dev"
    assert record["url"] == "https://acme.circle.so/c/x/1"
    assert record["author"]["display_name"] == "Dana"
    assert record["content_type"] == "post"


def test_absolute_urls_are_left_alone():
    record = normalize_post(
        {"id": 1, "body": "x", "url": "https://other.circle.so/p/1"},
        community_url="https://acme.circle.so",
    )
    assert record["url"] == "https://other.circle.so/p/1"


def test_normalize_comment_carries_thread_id():
    record = normalize_comment({"id": 7, "body": {"body": "yes"}}, post_id="101")
    assert record["thread_id"] == "101"
    assert record["content_type"] == "comment"


def test_normalize_chat_message_uses_room_uuid():
    record = normalize_chat_message({"id": 3, "body": "hello"}, chat_room_uuid="room-1")
    assert record["thread_id"] == "room-1"
    assert record["content_type"] == "chat_message"


def test_parse_timestamp_handles_formats():
    assert parse_timestamp("2026-08-30T10:00:00Z") is not None
    assert parse_timestamp("not a date") is None
    assert parse_timestamp(None) is None
