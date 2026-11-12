"""Tests for the main Lambda handler orchestration logic."""

from datetime import datetime, timedelta, timezone
from unittest.mock import patch, MagicMock
from moto import mock_aws
from handler import lambda_handler, build_conversation_summary, load_property_context


@mock_aws
@patch("handler.HospitableClient")
@patch("handler.classify_message")
@patch("handler.draft_response")
@patch("handler.summarize_conversation")
@patch("handler.post_guest_alert")
def test_handler_processes_new_guest_messages(
    mock_slack, mock_summary, mock_draft, mock_classify, mock_hospitable, dynamodb_table
):
    """Handler should process new guest messages and post to Slack."""
    # Set up mocks
    mock_client = MagicMock()
    mock_hospitable.return_value = mock_client
    mock_summary.return_value = "• *Agreed so far:* nothing yet\n• *Still open:* AC broken"

    mock_client.get_active_reservations.return_value = [
        {
            "id": "res-123",
            "property_id": "prop-1",
            "property_name": "Villa Bougainvillea",
            "check_in": "2026-04-20T16:00:00-07:00",
            "check_out": "2026-04-25T11:00:00-07:00",
            "status": "accepted",
            "platform": "airbnb",
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

    # Regression guard: Slack alert must receive non-empty check-in / check-out
    # dates (bug where code read `checkin`/`checkout` but API returns
    # `check_in`/`check_out` produced blank dates in the alert).
    call_kwargs = mock_slack.call_args.kwargs
    assert call_kwargs["checkin_date"] == "2026-04-20"
    assert call_kwargs["checkout_date"] == "2026-04-25"
    assert call_kwargs["booking_source"] == "airbnb"
    # Summary pipeline: handler computes and forwards the bullet summary.
    mock_summary.assert_called_once()
    assert "Still open" in call_kwargs["conversation_summary"]


@mock_aws
@patch("handler.HospitableClient")
def test_handler_skips_host_messages(mock_hospitable, dynamodb_table):
    """Handler should never alert on host/automated messages.

    The stay-channel mirror pass may still fire for a host message when the
    stay-channels feature is on (see test_poller_mirrors_host_messages_*),
    but STAY_CHANNELS_ENABLED defaults to False in the test env, so this
    test only guards the alert-pipeline invariant.
    """
    mock_client = MagicMock()
    mock_hospitable.return_value = mock_client

    mock_client.get_active_reservations.return_value = [
        {
            "id": "res-123",
            "property_id": "prop-1",
            "property_name": "Villa Bougainvillea",
            "check_in": "2026-04-20T16:00:00-07:00",
            "check_out": "2026-04-25T11:00:00-07:00",
            "status": "accepted",
            "platform": "airbnb",
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
@patch("handler.STAY_CHANNELS_ENABLED", True)
@patch("handler.StayChannelSlackClient")
@patch("handler.mirror_message")
@patch("handler.HospitableClient")
@patch("handler.classify_message")
@patch("handler.draft_response")
@patch("handler.summarize_conversation", return_value="")
@patch("handler.post_guest_alert")
def test_poller_mirrors_host_messages_in_conversation(
    mock_slack, mock_summary, mock_draft, mock_classify,
    mock_hospitable, mock_mirror, mock_stay_slack, dynamodb_table,
):
    """A [guest, host] pair must mirror both messages into the stay journal
    even when the guest message is marked ANSWERED (no alert posted). This is
    the fix for the ambient agent missing host replies sent from the PMS."""
    mock_client = MagicMock()
    mock_hospitable.return_value = mock_client

    mock_client.get_active_reservations.return_value = [
        {
            "id": "res-mirror-1",
            "property_id": "prop-1",
            "property_name": "Villa Bougainvillea",
            "check_in": "2026-04-20T16:00:00-07:00",
            "check_out": "2026-04-25T11:00:00-07:00",
            "status": "accepted",
            "platform": "airbnb",
            "guest": {"first_name": "Jane", "full_name": "Jane Smith"},
        }
    ]
    mock_client.get_reservation_messages.return_value = [
        {
            "id": "msg-guest",
            "body": "AC is broken",
            "sender_type": "guest",
            "source": "platform",
            "created_at": "2026-04-20T22:00:00Z",
        },
        {
            "id": "msg-host",
            "body": "Technician coming at 4pm",
            "sender_type": "host",
            "source": "manual",
            "created_at": "2026-04-20T22:30:00Z",
        },
    ]
    mock_client.get_property.return_value = {"description": "desc"}
    mock_client.get_property_knowledge_hub.return_value = {"topics": []}

    result = lambda_handler({}, None)

    import json as _json
    body = _json.loads(result["body"])
    # Guest is ANSWERED — no alert posted.
    assert body["alerts_posted"] == 0
    mock_slack.assert_not_called()

    # But BOTH messages mirror into the stay journal.
    assert mock_mirror.call_count == 2
    sender_types = [
        call.kwargs["message"].get("sender_type")
        for call in mock_mirror.call_args_list
    ]
    assert sender_types == ["guest", "host"]


@mock_aws
@patch("handler.STAY_CHANNELS_ENABLED", True)
@patch("handler.StayChannelSlackClient")
@patch("handler.mirror_message")
@patch("handler.HospitableClient")
@patch("handler.post_guest_alert")
def test_poller_mirrors_host_only_conversation(
    mock_slack, mock_hospitable, mock_mirror, mock_stay_slack, dynamodb_table,
):
    """Simulates a host-only message in the API response (e.g. webhook outage
    where the guest message was already mirrored and only the host reply
    remains unseen). Mirror fires once; alert pipeline is untouched."""
    mock_client = MagicMock()
    mock_hospitable.return_value = mock_client

    mock_client.get_active_reservations.return_value = [
        {
            "id": "res-host-only",
            "property_id": "prop-1",
            "property_name": "Villa Bougainvillea",
            "check_in": "2026-04-20T16:00:00-07:00",
            "check_out": "2026-04-25T11:00:00-07:00",
            "status": "accepted",
            "platform": "airbnb",
            "guest": {"first_name": "Jane", "full_name": "Jane Smith"},
        }
    ]
    mock_client.get_reservation_messages.return_value = [
        {
            "id": "msg-host-only",
            "body": "Odor spray arriving tomorrow 3-6 AM",
            "sender_type": "host",
            "source": "manual",
            "created_at": "2026-04-20T22:30:00Z",
        }
    ]
    mock_client.get_property.return_value = {"description": "desc"}
    mock_client.get_property_knowledge_hub.return_value = {"topics": []}

    lambda_handler({}, None)

    mock_mirror.assert_called_once()
    assert mock_mirror.call_args.kwargs["message"]["sender_type"] == "host"
    mock_slack.assert_not_called()


@mock_aws
@patch("handler.HospitableClient")
@patch("handler.classify_message")
@patch("handler.draft_response")
@patch("handler.summarize_conversation", return_value="")
@patch("handler.post_guest_alert")
def test_handler_does_not_reprocess_messages(
    mock_slack, mock_summary, mock_draft, mock_classify, mock_hospitable, dynamodb_table
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
@patch("handler.summarize_conversation", return_value="")
@patch("handler.post_guest_alert")
def test_handler_resolves_property_name_from_kb(
    mock_slack, mock_summary, mock_draft, mock_classify, mock_hospitable, dynamodb_table
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
            "check_in": "2026-04-20T16:00:00-07:00",
            "check_out": "2026-04-25T11:00:00-07:00",
            "status": "accepted",
            "platform": "airbnb",
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


@mock_aws
@patch("handler.HospitableClient")
@patch("handler.classify_message")
@patch("handler.draft_response")
@patch("handler.summarize_conversation", return_value="")
@patch("handler.post_guest_alert")
def test_handler_handles_null_body_without_crashing(
    mock_slack, mock_summary, mock_draft, mock_classify, mock_hospitable, dynamodb_table
):
    """Messages with body=None (attachments, etc.) must not crash the handler."""
    mock_client = MagicMock()
    mock_hospitable.return_value = mock_client

    mock_client.get_active_reservations.return_value = [
        {
            "id": "res-null-body",
            "property_id": "f8236d9d-988a-4192-9d16-2927b0b9ad8e",
            "check_in": "2026-04-20T16:00:00-07:00",
            "check_out": "2026-04-25T11:00:00-07:00",
            "status": "accepted",
            "platform": "airbnb",
            "guest": {"first_name": "Jane", "full_name": "Jane Smith"},
        }
    ]
    # Two messages: one with null body (attachment), one normal
    mock_client.get_reservation_messages.return_value = [
        {
            "id": "msg-attachment",
            "body": None,
            "sender_type": "guest",
            "source": "platform",
            "created_at": "2026-04-20T22:00:00Z",
        },
        {
            "id": "msg-real",
            "body": "Hi!",
            "sender_type": "guest",
            "source": "platform",
            "created_at": "2026-04-20T22:01:00Z",
        },
    ]
    mock_client.get_property.return_value = {"description": "desc"}
    mock_client.get_property_knowledge_hub.return_value = {"topics": []}

    mock_classify.return_value = {"category": "GENERAL", "urgency": "LOW", "summary": "hi"}
    mock_draft.return_value = "Hi there!"
    mock_slack.return_value = {"ok": True}

    # Must complete without exceptions
    import json as _json
    result = lambda_handler({}, None)
    body = _json.loads(result["body"])
    assert body["errors"] == []
    # Real message posts to Slack; attachment is marked as empty + skipped
    assert body["alerts_posted"] == 1


@mock_aws
@patch("handler.HospitableClient")
@patch("handler.classify_message")
@patch("handler.draft_response")
@patch("handler.summarize_conversation", return_value="")
@patch("handler.post_guest_alert")
def test_poller_skips_message_with_later_host_reply(
    mock_slack, mock_summary, mock_draft, mock_classify, mock_hospitable, dynamodb_table
):
    """If the host has already replied to a guest message, the poller must
    not alert on it. Covers the common race where VJ or Maggie replies in
    the Hospitable app faster than the hourly poll runs."""
    mock_client = MagicMock()
    mock_hospitable.return_value = mock_client

    mock_client.get_active_reservations.return_value = [
        {
            "id": "res-123",
            "property_id": "prop-1",
            "property_name": "Villa Bougainvillea",
            "check_in": "2026-04-20T16:00:00-07:00",
            "check_out": "2026-04-25T11:00:00-07:00",
            "status": "accepted",
            "platform": "airbnb",
            "guest": {"first_name": "Jane", "full_name": "Jane Smith"},
        }
    ]
    # Oldest → newest. Guest asks, then host replies manually.
    mock_client.get_reservation_messages.return_value = [
        {
            "id": "msg-guest",
            "body": "The AC is broken",
            "sender_type": "guest",
            "source": "platform",
            "created_at": "2026-04-20T22:00:00Z",
        },
        {
            "id": "msg-host",
            "body": "Technician coming at 4pm",
            "sender_type": "host",
            "source": "manual",
            "created_at": "2026-04-20T22:30:00Z",
        },
    ]
    mock_client.get_property.return_value = {"description": "desc"}
    mock_client.get_property_knowledge_hub.return_value = {"topics": []}

    result = lambda_handler({}, None)

    import json as _json
    body = _json.loads(result["body"])
    assert body["new_messages_found"] == 0
    assert body["alerts_posted"] == 0
    mock_slack.assert_not_called()


@mock_aws
@patch("handler.HospitableClient")
@patch("handler.classify_message")
@patch("handler.draft_response")
@patch("handler.summarize_conversation", return_value="")
@patch("handler.post_guest_alert")
def test_poller_still_alerts_when_only_automated_host_reply(
    mock_slack, mock_summary, mock_draft, mock_classify, mock_hospitable, dynamodb_table
):
    """An automated welcome / checkout message isn't a human reply, so the
    poller should still surface the guest message for VJ/Maggie to look at."""
    mock_client = MagicMock()
    mock_hospitable.return_value = mock_client

    mock_client.get_active_reservations.return_value = [
        {
            "id": "res-123",
            "property_id": "prop-1",
            "property_name": "Villa Bougainvillea",
            "check_in": "2026-04-20T16:00:00-07:00",
            "check_out": "2026-04-25T11:00:00-07:00",
            "status": "accepted",
            "platform": "airbnb",
            "guest": {"first_name": "Jane", "full_name": "Jane Smith"},
        }
    ]
    mock_client.get_reservation_messages.return_value = [
        {
            "id": "msg-guest",
            "body": "The AC is broken",
            "sender_type": "guest",
            "source": "platform",
            "created_at": "2026-04-20T22:00:00Z",
        },
        {
            "id": "msg-auto",
            "body": "Thanks for staying with us!",
            "sender_type": "host",
            "source": "automated",
            "created_at": "2026-04-20T22:01:00Z",
        },
    ]
    mock_client.get_property.return_value = {"description": "desc"}
    mock_client.get_property_knowledge_hub.return_value = {"topics": []}
    mock_classify.return_value = {"category": "URGENT_MAINTENANCE", "urgency": "HIGH", "summary": "AC"}
    mock_draft.return_value = "Draft"
    mock_slack.return_value = {"ok": True}

    result = lambda_handler({}, None)
    import json as _json
    body = _json.loads(result["body"])
    assert body["alerts_posted"] == 1


@mock_aws
@patch("handler.HospitableClient")
@patch("handler.classify_message")
@patch("handler.draft_response")
@patch("handler.summarize_conversation", return_value="")
@patch("handler.post_guest_alert")
def test_poller_ignores_messages_older_than_lookback_window(
    mock_slack, mock_summary, mock_draft, mock_classify, mock_hospitable,
    dynamodb_table, monkeypatch,
):
    """The lookback guard prevents a flood of alerts on the first poll after
    deploy. A message older than POLLER_LOOKBACK_HOURS is left alone."""
    monkeypatch.setenv("POLLER_LOOKBACK_HOURS", "6")

    now = datetime.now(timezone.utc)
    old_ts = (now - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")

    mock_client = MagicMock()
    mock_hospitable.return_value = mock_client
    mock_client.get_active_reservations.return_value = [
        {
            "id": "res-123",
            "property_id": "prop-1",
            "property_name": "Villa Bougainvillea",
            "check_in": "2026-04-20T16:00:00-07:00",
            "check_out": "2026-04-25T11:00:00-07:00",
            "status": "accepted",
            "platform": "airbnb",
            "guest": {"first_name": "Jane", "full_name": "Jane Smith"},
        }
    ]
    mock_client.get_reservation_messages.return_value = [
        {
            "id": "msg-stale",
            "body": "A day-old question",
            "sender_type": "guest",
            "source": "platform",
            "created_at": old_ts,
        }
    ]
    mock_client.get_property.return_value = {"description": "desc"}
    mock_client.get_property_knowledge_hub.return_value = {"topics": []}

    result = lambda_handler({}, None)
    import json as _json
    body = _json.loads(result["body"])
    assert body["new_messages_found"] == 0
    assert body["alerts_posted"] == 0
    mock_slack.assert_not_called()


@mock_aws
@patch("handler.HospitableClient")
@patch("handler.classify_message")
@patch("handler.draft_response")
@patch("handler.summarize_conversation", return_value="")
@patch("handler.post_guest_alert")
def test_poller_alerts_recent_messages_within_window(
    mock_slack, mock_summary, mock_draft, mock_classify, mock_hospitable,
    dynamodb_table, monkeypatch,
):
    """Companion to the lookback test: a message inside the window still gets
    alerted — proves the gate isn't dropping everything."""
    monkeypatch.setenv("POLLER_LOOKBACK_HOURS", "6")

    now = datetime.now(timezone.utc)
    recent_ts = (now - timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ")

    mock_client = MagicMock()
    mock_hospitable.return_value = mock_client
    mock_client.get_active_reservations.return_value = [
        {
            "id": "res-123",
            "property_id": "prop-1",
            "property_name": "Villa Bougainvillea",
            "check_in": "2026-04-20T16:00:00-07:00",
            "check_out": "2026-04-25T11:00:00-07:00",
            "status": "accepted",
            "platform": "airbnb",
            "guest": {"first_name": "Jane", "full_name": "Jane Smith"},
        }
    ]
    mock_client.get_reservation_messages.return_value = [
        {
            "id": "msg-recent",
            "body": "The AC is broken",
            "sender_type": "guest",
            "source": "platform",
            "created_at": recent_ts,
        }
    ]
    mock_client.get_property.return_value = {"description": "desc"}
    mock_client.get_property_knowledge_hub.return_value = {"topics": []}
    mock_classify.return_value = {"category": "URGENT_MAINTENANCE", "urgency": "HIGH", "summary": "AC"}
    mock_draft.return_value = "Draft"
    mock_slack.return_value = {"ok": True}

    result = lambda_handler({}, None)
    import json as _json
    body = _json.loads(result["body"])
    assert body["alerts_posted"] == 1


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


def test_load_property_context_does_not_fetch_live_hub():
    """Regression: the live Hospitable Hub fetch was removed. The Hub now
    reaches the drafter via the sibling snapshot (loaded by
    knowledge_base_loader), not via a per-request API call."""
    mock_client = MagicMock()
    mock_client.get_property.return_value = {"description": "a nice place"}

    context = load_property_context(mock_client, "test-prop-uuid-1")

    mock_client.get_property_knowledge_hub.assert_not_called()
    # Backward-compat key still present for callers that pass it through.
    assert context["knowledge_hub"] == ""
    assert context["description"] == "a nice place"
