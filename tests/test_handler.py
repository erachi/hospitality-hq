"""Tests for the main Lambda handler orchestration logic."""

from unittest.mock import patch, MagicMock
from moto import mock_aws
from handler import lambda_handler, build_conversation_summary, load_property_context


@mock_aws
@patch("handler.HospitableClient")
@patch("handler.classify_message")
@patch("handler.draft_response")
@patch("handler.post_guest_alert")
def test_handler_processes_new_guest_messages(
    mock_slack, mock_draft, mock_classify, mock_hospitable, dynamodb_table
):
    """Handler should process new guest messages and post to Slack."""
    # Set up mocks
    mock_client = MagicMock()
    mock_hospitable.return_value = mock_client

    mock_client.get_active_reservations.return_value = [
        {
            "id": "res-123",
            "property_id": "prop-1",
            "property_name": "Villa Bougainvillea",
            "checkin": "2026-04-20",
            "checkout": "2026-04-25",
            "guest": {"first_name": "Jane", "full_name": "Jane Smith"},
        }
    ]

    mock_client.get_reservation_messages.return_value = [
        {
            "id": "msg-001",
            "body": "The AC is broken",
            "sender_type": "guest",
            "source": "platform",
            "created_at": "2026-04-20T22:00:00Z",
        }
    ]

    mock_client.get_property.return_value = {"description": "A beautiful property"}
    mock_client.get_property_knowledge_hub.return_value = {"topics": []}

    mock_classify.return_value = {
        "category": "URGENT_MAINTENANCE",
        "urgency": "HIGH",
        "summary": "AC broken",
    }
    mock_draft.return_value = "Hi Jane, sorry about the AC!"
    mock_slack.return_value = {"ok": True}

    # Run handler
    result = lambda_handler({}, None)

    assert result["statusCode"] == 200

    import json
    body = json.loads(result["body"])
    assert body["reservations_checked"] == 1
    assert body["new_messages_found"] == 1
    assert body["alerts_posted"] == 1

    # Verify Slack was called
    mock_slack.assert_called_once()


@mock_aws
@patch("handler.HospitableClient")
def test_handler_skips_host_messages(mock_hospitable, dynamodb_table):
    """Handler should ignore host/automated messages."""
    mock_client = MagicMock()
    mock_hospitable.return_value = mock_client

    mock_client.get_active_reservations.return_value = [
        {
            "id": "res-123",
            "property_id": "prop-1",
            "property_name": "Villa Bougainvillea",
            "checkin": "2026-04-20",
            "checkout": "2026-04-25",
            "guest": {"first_name": "Jane", "full_name": "Jane Smith"},
        }
    ]

    mock_client.get_reservation_messages.return_value = [
        {
            "id": "msg-001",
            "body": "Welcome to Villa Bougainvillea!",
            "sender_type": "host",
            "source": "automated",
            "created_at": "2026-04-20T16:00:00Z",
        }
    ]

    result = lambda_handler({}, None)

    import json
    body = json.loads(result["body"])
    assert body["new_messages_found"] == 0
    assert body["alerts_posted"] == 0


@mock_aws
@patch("handler.HospitableClient")
@patch("handler.classify_message")
@patch("handler.draft_response")
@patch("handler.post_guest_alert")
def test_handler_does_not_reprocess_messages(
    mock_slack, mock_draft, mock_classify, mock_hospitable, dynamodb_table
):
    """Handler should skip already-processed messages on second run."""
    mock_client = MagicMock()
    mock_hospitable.return_value = mock_client

    reservation = {
        "id": "res-123",
        "property_id": "prop-1",
        "property_name": "Villa Bougainvillea",
        "checkin": "2026-04-20",
        "checkout": "2026-04-25",
        "guest": {"first_name": "Jane", "full_name": "Jane Smith"},
    }

    messages = [
        {
            "id": "msg-001",
            "body": "The AC is broken",
            "sender_type": "guest",
            "source": "platform",
            "created_at": "2026-04-20T22:00:00Z",
        }
    ]

    mock_client.get_active_reservations.return_value = [reservation]
    mock_client.get_reservation_messages.return_value = messages
    mock_client.get_property.return_value = {"description": "desc"}
    mock_client.get_property_knowledge_hub.return_value = {"topics": []}
    mock_classify.return_value = {"category": "URGENT_MAINTENANCE", "urgency": "HIGH", "summary": "AC broken"}
    mock_draft.return_value = "Draft response"
    mock_slack.return_value = {"ok": True}

    # First run — should process
    result1 = lambda_handler({}, None)
    import json
    body1 = json.loads(result1["body"])
    assert body1["alerts_posted"] == 1

    # Second run — same messages, should skip
    mock_slack.reset_mock()
    result2 = lambda_handler({}, None)
    body2 = json.loads(result2["body"])
    assert body2["new_messages_found"] == 0
    assert body2["alerts_posted"] == 0


@mock_aws
@patch("handler.HospitableClient")
@patch("handler.classify_message")
@patch("handler.draft_response")
@patch("handler.post_guest_alert")
def test_handler_resolves_property_name_from_kb(
    mock_slack, mock_draft, mock_classify, mock_hospitable, dynamodb_table
):
    """When reservation lacks property_name, handler should resolve it from the local KB."""
    mock_client = MagicMock()
    mock_hospitable.return_value = mock_client

    # Reservation uses Villa Bougainvillea's real UUID but has NO property_name —
    # which is the real-world Hospitable API behavior that caused "Unknown Property" in logs.
    mock_client.get_active_reservations.return_value = [
        {
            "id": "res-123",
            "property_id": "f8236d9d-988a-4192-9d16-2927b0b9ad8e",
            "checkin": "2026-04-20",
            "checkout": "2026-04-25",
            "guest": {"first_name": "Jane", "full_name": "Jane Smith"},
        }
    ]
    mock_client.get_reservation_messages.return_value = [
        {
            "id": "msg-x",
            "body": "Hi!",
            "sender_type": "guest",
            "source": "platform",
            "created_at": "2026-04-20T22:00:00Z",
        }
    ]
    mock_client.get_property.return_value = {"description": "desc"}
    mock_client.get_property_knowledge_hub.return_value = {"topics": []}

    mock_classify.return_value = {"category": "GENERAL", "urgency": "LOW", "summary": "hi"}
    mock_draft.return_value = "Hi!"
    mock_slack.return_value = {"ok": True}

    lambda_handler({}, None)

    # Slack should receive the canonical name from our KB, not "Unknown Property"
    call_kwargs = mock_slack.call_args.kwargs
    assert call_kwargs["property_name"] == "Villa Bougainvillea"


def test_build_conversation_summary():
    """Should build a readable summary of recent messages."""
    messages = [
        {"sender_type": "guest", "body": "Hi, when can we check in?"},
        {"sender_type": "host", "body": "Check-in is at 4 PM!"},
        {"sender_type": "guest", "body": "Great, thanks!"},
    ]

    summary = build_conversation_summary(messages)

    assert "[GUEST] Hi, when can we check in?" in summary
    assert "[HOST] Check-in is at 4 PM!" in summary
    assert "[GUEST] Great, thanks!" in summary


def test_build_conversation_summary_truncates_long_messages():
    """Long messages should be truncated in the summary."""
    messages = [
        {"sender_type": "guest", "body": "x" * 500},
    ]

    summary = build_conversation_summary(messages)

    assert "..." in summary
    assert len(summary) < 500
