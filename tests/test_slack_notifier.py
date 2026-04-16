"""Tests for Slack notification formatting and posting."""

import responses
from unittest.mock import patch
from slack_notifier import post_guest_alert


@responses.activate
@patch("slack_notifier.get_slack_bot_token", return_value="xoxb-test-token")
def test_post_guest_alert_urgent(mock_token):
    """Urgent maintenance alert should include emergency indicators."""
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
        },
        draft_response="Hi Jane! I'm so sorry about the AC...",
        reservation_uuid="res-uuid-123",
    )

    assert result["ok"] is True

    # Verify the request was made correctly
    request_body = responses.calls[0].request.body
    if isinstance(request_body, bytes):
        request_body = request_body.decode("utf-8")
    assert "Villa Bougainvillea" in request_body
    assert "Jane Smith" in request_body


@responses.activate
@patch("slack_notifier.get_slack_bot_token", return_value="xoxb-test-token")
def test_post_guest_alert_positive(mock_token):
    """Positive feedback should not include send instructions."""
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
        },
        draft_response="So glad you enjoyed it!",
        reservation_uuid="res-uuid-456",
    )

    assert result["ok"] is True

    request_body = responses.calls[0].request.body
    if isinstance(request_body, bytes):
        request_body = request_body.decode("utf-8")
    assert "No response needed" in request_body


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
