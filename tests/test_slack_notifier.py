"""Tests for Slack notification formatting and posting."""

import json

import responses
from unittest.mock import patch
from slack_notifier import post_guest_alert


def _request_payload(call) -> dict:
    """Decode the JSON body of a recorded request."""
    body = call.request.body
    if isinstance(body, bytes):
        body = body.decode("utf-8")
    return json.loads(body)


def _blocks_text(payload: dict) -> str:
    """Concatenate every text value across all blocks for easy substring assertions."""
    parts = []
    for block in payload.get("blocks", []):
        text = block.get("text")
        if isinstance(text, dict):
            parts.append(text.get("text", ""))
        for field in block.get("fields", []) or []:
            parts.append(field.get("text", ""))
        for el in block.get("elements", []) or []:
            parts.append(el.get("text", ""))
    return "\n".join(parts)


@responses.activate
@patch("slack_notifier.get_slack_bot_token", return_value="xoxb-test-token")
def test_post_guest_alert_urgent_uses_block_kit(mock_token):
    """Urgent maintenance alert should post Block Kit blocks with booking context."""
    responses.add(
        responses.POST,
        "https://slack.com/api/chat.postMessage",
        json={"ok": True, "ts": "1234567890.123456"},
        status=200,
    )

    result = post_guest_alert(
        guest_name="Jane Smith",
        property_name="Villa Bougainvillea",
        checkin_date="2026-04-20",
        checkout_date="2026-04-25",
        guest_message="The AC isn't working and it's really hot!",
        classification={
            "category": "URGENT_MAINTENANCE",
            "urgency": "HIGH",
            "summary": "AC not working",
            "descriptor": "AC Not Working",
        },
        draft_response="Hi Jane! I'm so sorry about the AC...",
        reservation_uuid="res-uuid-123",
        booking_source="airbnb",
        reservation_status="checkpoint",
    )

    assert result["ok"] is True

    payload = _request_payload(responses.calls[0])
    assert "blocks" in payload
    assert isinstance(payload["blocks"], list) and len(payload["blocks"]) > 0
    assert payload["unfurl_links"] is False
    assert payload["unfurl_media"] is False

    # Plain-text fallback for mobile notifications — descriptor, not category label.
    assert "AC Not Working" in payload["text"]
    assert "Villa Bougainvillea" in payload["text"]

    text = _blocks_text(payload)
    # Header uses the uppercased descriptor + property, flanked with 🚨 for HIGH.
    header_block = payload["blocks"][0]
    assert header_block["type"] == "header"
    header_text = header_block["text"]["text"]
    assert "AC NOT WORKING" in header_text
    assert "Villa Bougainvillea" in header_text
    assert "🚨" in header_text
    # The old category-emoji combo (🔧) should no longer appear in the header.
    assert "🔧" not in header_text

    assert "Jane Smith" in text
    assert "2026-04-20" in text and "2026-04-25" in text
    # Source + status should be human-friendly.
    assert "Airbnb" in text
    assert "Checked In" in text
    # Reservation id appears in the footer.
    assert "res-uuid-123" in text
    # New guest badge by default.
    assert "(New)" in text


@responses.activate
@patch("slack_notifier.get_slack_bot_token", return_value="xoxb-test-token")
def test_post_guest_alert_header_non_high_urgency(mock_token):
    """Non-HIGH urgency uses a plain urgency dot, not the 🚨 flanking."""
    responses.add(
        responses.POST,
        "https://slack.com/api/chat.postMessage",
        json={"ok": True, "ts": "1234567890.123456"},
        status=200,
    )

    post_guest_alert(
        guest_name="Amrit",
        property_name="Villa Bougainvillea",
        checkin_date="2026-04-29",
        checkout_date="2026-05-03",
        guest_message="Can we check in at 11?",
        classification={
            "category": "PRE_ARRIVAL",
            "urgency": "LOW",
            "summary": "Early check-in request",
            "descriptor": "Early Check-in Request",
        },
        draft_response="Let me check on that!",
        reservation_uuid="res-uuid-999",
    )

    payload = _request_payload(responses.calls[0])
    header_text = payload["blocks"][0]["text"]["text"]
    assert "EARLY CHECK-IN REQUEST" in header_text
    assert "Villa Bougainvillea" in header_text
    assert "🟢" in header_text  # LOW urgency dot
    assert "🚨" not in header_text


@responses.activate
@patch("slack_notifier.get_slack_bot_token", return_value="xoxb-test-token")
def test_post_guest_alert_header_falls_back_without_descriptor(mock_token):
    """Missing descriptor falls back to the category label so the header still reads sensibly."""
    responses.add(
        responses.POST,
        "https://slack.com/api/chat.postMessage",
        json={"ok": True, "ts": "1234567890.123456"},
        status=200,
    )

    post_guest_alert(
        guest_name="Test",
        property_name="The Palm Club",
        checkin_date="2026-04-20",
        checkout_date="2026-04-25",
        guest_message="hello",
        classification={"category": "GENERAL", "urgency": "MEDIUM", "summary": "hi"},
        draft_response="hi",
        reservation_uuid="res-fallback",
    )

    payload = _request_payload(responses.calls[0])
    header_text = payload["blocks"][0]["text"]["text"]
    assert "GENERAL INQUIRY" in header_text
    assert "The Palm Club" in header_text


@responses.activate
@patch("slack_notifier.get_slack_bot_token", return_value="xoxb-test-token")
def test_post_guest_alert_no_response_needed(mock_token):
    """When the classifier says response_needed=False, footer suppresses the send call-to-action."""
    responses.add(
        responses.POST,
        "https://slack.com/api/chat.postMessage",
        json={"ok": True, "ts": "1234567890.123456"},
        status=200,
    )

    result = post_guest_alert(
        guest_name="John Doe",
        property_name="The Palm Club",
        checkin_date="2026-04-18",
        checkout_date="2026-04-22",
        guest_message="We had an amazing time, thank you!",
        classification={
            "category": "POSITIVE",
            "urgency": "LOW",
            "summary": "Guest enjoyed their stay",
            "response_needed": False,
        },
        draft_response="So glad you enjoyed it!",
        reservation_uuid="res-uuid-456",
    )

    assert result["ok"] is True

    payload = _request_payload(responses.calls[0])
    text = _blocks_text(payload)
    assert "No response needed" in text
    assert "Copy draft" not in text


@responses.activate
@patch("slack_notifier.get_slack_bot_token", return_value="xoxb-test-token")
def test_post_guest_alert_positive_with_embedded_request_prompts_reply(mock_token):
    """POSITIVE category + response_needed=True must still prompt the host to send a draft.

    Regression guard: footer used to check `category == POSITIVE`, which
    silenced the call-to-action on messages like "thanks! one more q…".
    """
    responses.add(
        responses.POST,
        "https://slack.com/api/chat.postMessage",
        json={"ok": True, "ts": "1234567890.123456"},
        status=200,
    )

    post_guest_alert(
        guest_name="Amrit",
        property_name="Villa Bougainvillea",
        checkin_date="2026-04-29",
        checkout_date="2026-05-03",
        guest_message="thank you so much! by the way, can we check in at 11?",
        classification={
            "category": "POSITIVE",
            "urgency": "LOW",
            "summary": "Thanks + early check-in request",
            "response_needed": True,
        },
        draft_response="Let me check on early check-in for you!",
        reservation_uuid="res-uuid-789",
    )

    payload = _request_payload(responses.calls[0])
    text = _blocks_text(payload)
    assert "Copy draft" in text
    assert "No response needed" not in text


@responses.activate
@patch("slack_notifier.get_slack_bot_token", return_value="xoxb-test-token")
def test_post_guest_alert_defaults_to_response_needed(mock_token):
    """If classification lacks response_needed, default to showing the send CTA."""
    responses.add(
        responses.POST,
        "https://slack.com/api/chat.postMessage",
        json={"ok": True, "ts": "1234567890.123456"},
        status=200,
    )

    post_guest_alert(
        guest_name="Test",
        property_name="Test Property",
        checkin_date="2026-04-20",
        checkout_date="2026-04-25",
        guest_message="Test",
        classification={"category": "GENERAL", "urgency": "LOW", "summary": "Test"},
        draft_response="Test",
        reservation_uuid="res-default",
    )

    payload = _request_payload(responses.calls[0])
    text = _blocks_text(payload)
    assert "Copy draft" in text


@responses.activate
@patch("slack_notifier.get_slack_bot_token", return_value="xoxb-test-token")
def test_slack_auth_header(mock_token):
    """Should use the bot token in the Authorization header."""
    responses.add(
        responses.POST,
        "https://slack.com/api/chat.postMessage",
        json={"ok": True, "ts": "1234567890.123456"},
        status=200,
    )

    post_guest_alert(
        guest_name="Test",
        property_name="Test Property",
        checkin_date="2026-04-20",
        checkout_date="2026-04-25",
        guest_message="Test message",
        classification={"category": "GENERAL", "urgency": "LOW", "summary": "Test"},
        draft_response="Test response",
        reservation_uuid="res-test",
    )

    auth_header = responses.calls[0].request.headers["Authorization"]
    assert auth_header == "Bearer xoxb-test-token"


@responses.activate
@patch("slack_notifier.get_slack_bot_token", return_value="xoxb-test-token")
def test_post_guest_alert_with_message_history(mock_token):
    """Recent messages should render in the alert and the trigger should be bolded."""
    responses.add(
        responses.POST,
        "https://slack.com/api/chat.postMessage",
        json={"ok": True, "ts": "1234567890.123456"},
        status=200,
    )

    trigger = "The AC isn't working and it's really hot!"
    recent = [
        {
            "sender_type": "guest",
            "body": "Hi, checking in at 4?",
            "created_at": "2026-04-20T20:00:00Z",
        },
        {
            "sender_type": "host",
            "body": "Yes, 4pm works!",
            "created_at": "2026-04-20T20:05:00Z",
        },
        {
            "sender_type": "guest",
            "body": trigger,
            "created_at": "2026-04-20T22:00:00Z",
        },
    ]

    post_guest_alert(
        guest_name="Jane Smith",
        property_name="Villa Bougainvillea",
        checkin_date="2026-04-20",
        checkout_date="2026-04-25",
        guest_message=trigger,
        classification={
            "category": "URGENT_MAINTENANCE",
            "urgency": "HIGH",
            "summary": "AC not working",
        },
        draft_response="Sending someone over right now.",
        reservation_uuid="res-uuid-789",
        booking_source="airbnb",
        reservation_status="checkpoint",
        is_repeat_guest=True,
        recent_messages=recent,
    )

    payload = _request_payload(responses.calls[0])
    text = _blocks_text(payload)

    # The triggering message should appear in its own "NEW MESSAGE" section.
    assert "NEW MESSAGE" in text
    assert trigger in text
    # Prior messages should appear with GUEST/HOST labels in the history.
    assert "[GUEST]" in text
    assert "[HOST]" in text
    assert "Hi, checking in at 4?" in text
    assert "Yes, 4pm works!" in text
    # The trigger should NOT be duplicated in the conversation history.
    # It appears once in NEW MESSAGE, and the history excludes it.
    assert text.count(trigger) == 1
    # Repeat guest badge should flip.
    assert "(Repeat)" in text
    assert "(New)" not in text


@responses.activate
@patch("slack_notifier.get_slack_bot_token", return_value="xoxb-test-token")
def test_post_guest_alert_without_history(mock_token):
    """With no recent messages, the history section should show the empty placeholder."""
    responses.add(
        responses.POST,
        "https://slack.com/api/chat.postMessage",
        json={"ok": True, "ts": "1234567890.123456"},
        status=200,
    )

    post_guest_alert(
        guest_name="Jane Smith",
        property_name="Villa Bougainvillea",
        checkin_date="2026-04-20",
        checkout_date="2026-04-25",
        guest_message="Hello",
        classification={"category": "GENERAL", "urgency": "LOW", "summary": "Hi"},
        draft_response="Hi back",
        reservation_uuid="res-empty",
        recent_messages=[],
    )

    payload = _request_payload(responses.calls[0])
    text = _blocks_text(payload)
    assert "No prior messages" in text
