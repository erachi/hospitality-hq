"""Tests for the webhook handler Lambda function."""

import hashlib
import hmac
import json
from unittest.mock import patch, MagicMock

from moto import mock_aws

from webhook_handler import webhook_handler, verify_signature

WEBHOOK_SIGNING_SECRET = "test-webhook-secret-key"

SAMPLE_WEBHOOK_PAYLOAD = {
    "id": "webhook-evt-001",
    "action": "message.created",
    "data": {
        "platform": "airbnb",
        "reservation_id": "res-uuid-123",
        "body": "The AC isn't working and it's really hot",
        "sender_type": "guest",
        "sender": {"first_name": "Jane", "full_name": "Jane Smith"},
        "content_type": "text/plain",
        "source": "platform",
        "created_at": "2026-04-20T22:15:00Z",
    },
    "created": "2026-04-20T22:15:00Z",
    "version": "1.0",
}


def _make_webhook_event(payload_dict: dict, secret: str = WEBHOOK_SIGNING_SECRET) -> dict:
    """Build an API Gateway proxy event with a valid HMAC signature."""
    body = json.dumps(payload_dict)
    signature = hmac.new(
        secret.encode("utf-8"),
        body.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return {
        "body": body,
        "isBase64Encoded": False,
        "headers": {"Signature": signature},
    }


def _make_unsigned_webhook_event(payload_dict: dict) -> dict:
    """Build an API Gateway proxy event without a signature header."""
    return {
        "body": json.dumps(payload_dict),
        "isBase64Encoded": False,
        "headers": {},
    }


@mock_aws
@patch("webhook_handler.get_webhook_secret", return_value=WEBHOOK_SIGNING_SECRET)
@patch("webhook_handler.HospitableClient")
@patch("webhook_handler.classify_message")
@patch("webhook_handler.draft_response")
@patch("webhook_handler.summarize_conversation", return_value="")
@patch("webhook_handler.post_guest_alert")
def test_webhook_processes_guest_message(
    mock_slack, mock_summary, mock_draft, mock_classify, mock_hospitable, mock_secret,
    dynamodb_table,
):
    """Valid webhook with guest message should classify, draft, and post to Slack."""
    mock_client = MagicMock()
    mock_hospitable.return_value = mock_client

    mock_client.get_reservation_detail.return_value = {
        "id": "res-uuid-123",
        "platform": "airbnb",
        "status": "accepted",
        "check_in": "2026-04-20T16:00:00-07:00",
        "check_out": "2026-04-25T11:00:00-07:00",
        "guest": {"first_name": "Jane", "full_name": "Jane Smith"},
        "properties": [
            {"id": "prop-1", "name": "Villa Bougainvillea"},
        ],
    }
    mock_client.get_reservation_messages.return_value = [
        {"sender_type": "guest", "body": "The AC isn't working", "id": "msg-1"}
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

    event = _make_webhook_event(SAMPLE_WEBHOOK_PAYLOAD)
    result = webhook_handler(event, None)

    assert result["statusCode"] == 200
    body = json.loads(result["body"])
    assert body["status"] == "processed"

    mock_classify.assert_called_once()
    mock_draft.assert_called_once()
    mock_slack.assert_called_once()

    # Regression guard: the webhook path was posting "Unknown Property" with
    # blank dates because the code read `property_name`/`checkin`/`checkout`
    # from the reservation detail, but Hospitable returns properties under a
    # nested `properties[]` array and dates under `check_in`/`check_out`.
    slack_kwargs = mock_slack.call_args.kwargs
    assert slack_kwargs["property_name"] == "Villa Bougainvillea"
    assert slack_kwargs["checkin_date"] == "2026-04-20"
    assert slack_kwargs["checkout_date"] == "2026-04-25"
    assert slack_kwargs["booking_source"] == "airbnb"
    assert slack_kwargs["reservation_status"] == "accepted"


@mock_aws
@patch("webhook_handler.get_webhook_secret", return_value="")
@patch("webhook_handler.HospitableClient")
@patch("webhook_handler.classify_message")
@patch("webhook_handler.draft_response")
@patch("webhook_handler.summarize_conversation", return_value="")
@patch("webhook_handler.post_guest_alert")
def test_webhook_accepts_without_secret(
    mock_slack, mock_summary, mock_draft, mock_classify, mock_hospitable, mock_secret,
    dynamodb_table,
):
    """When no signing secret is configured, webhooks should be accepted without signature."""
    mock_client = MagicMock()
    mock_hospitable.return_value = mock_client

    mock_client.get_reservation_detail.return_value = {
        "id": "res-uuid-123",
        "platform": "airbnb",
        "status": "accepted",
        "check_in": "2026-04-20T16:00:00-07:00",
        "check_out": "2026-04-25T11:00:00-07:00",
        "guest": {"first_name": "Jane", "full_name": "Jane Smith"},
        "properties": [
            {"id": "prop-1", "name": "Villa Bougainvillea"},
        ],
    }
    mock_client.get_reservation_messages.return_value = []
    mock_client.get_property.return_value = {"description": "desc"}
    mock_client.get_property_knowledge_hub.return_value = {"topics": []}

    mock_classify.return_value = {
        "category": "URGENT_MAINTENANCE",
        "urgency": "HIGH",
        "summary": "AC broken",
    }
    mock_draft.return_value = "Draft response"
    mock_slack.return_value = {"ok": True}

    # No signature header at all
    event = _make_unsigned_webhook_event(SAMPLE_WEBHOOK_PAYLOAD)
    result = webhook_handler(event, None)

    assert result["statusCode"] == 200
    body = json.loads(result["body"])
    assert body["status"] == "processed"
    mock_slack.assert_called_once()


@mock_aws
@patch("webhook_handler.get_webhook_secret", return_value=WEBHOOK_SIGNING_SECRET)
def test_webhook_rejects_invalid_signature(mock_secret, dynamodb_table):
    """Webhook with invalid signature should return 401 when secret is configured."""
    event = {
        "body": json.dumps({"action": "message.created", "data": {}}),
        "isBase64Encoded": False,
        "headers": {"Signature": "invalid-signature"},
    }

    result = webhook_handler(event, None)

    assert result["statusCode"] == 401


@mock_aws
@patch("webhook_handler.get_webhook_secret", return_value=WEBHOOK_SIGNING_SECRET)
def test_webhook_ignores_non_guest_message(mock_secret, dynamodb_table):
    """Non-guest messages should be acknowledged but not processed."""
    payload = {
        "id": "webhook-evt-002",
        "action": "message.created",
        "data": {
            "reservation_id": "res-123",
            "body": "Welcome!",
            "sender_type": "host",
        },
    }
    event = _make_webhook_event(payload)

    result = webhook_handler(event, None)

    assert result["statusCode"] == 200
    body = json.loads(result["body"])
    assert body["status"] == "ignored"
    assert body["reason"] == "not guest message"


@mock_aws
@patch("webhook_handler.get_webhook_secret", return_value=WEBHOOK_SIGNING_SECRET)
def test_webhook_ignores_non_message_event(mock_secret, dynamodb_table):
    """Non message.created events should be acknowledged but not processed."""
    payload = {
        "id": "webhook-evt-003",
        "action": "reservation.created",
        "data": {"reservation_id": "res-123"},
    }
    event = _make_webhook_event(payload)

    result = webhook_handler(event, None)

    assert result["statusCode"] == 200
    body = json.loads(result["body"])
    assert body["status"] == "ignored"
    assert body["reason"] == "not message.created"


@mock_aws
@patch("webhook_handler.get_webhook_secret", return_value=WEBHOOK_SIGNING_SECRET)
@patch("webhook_handler.HospitableClient")
@patch("webhook_handler.classify_message")
@patch("webhook_handler.draft_response")
@patch("webhook_handler.summarize_conversation", return_value="")
@patch("webhook_handler.post_guest_alert")
def test_webhook_deduplicates(
    mock_slack, mock_summary, mock_draft, mock_classify, mock_hospitable, mock_secret,
    dynamodb_table,
):
    """Same webhook delivered twice should only process once."""
    mock_client = MagicMock()
    mock_hospitable.return_value = mock_client

    mock_client.get_reservation_detail.return_value = {
        "id": "res-uuid-123",
        "platform": "airbnb",
        "status": "accepted",
        "check_in": "2026-04-20T16:00:00-07:00",
        "check_out": "2026-04-25T11:00:00-07:00",
        "guest": {"first_name": "Jane", "full_name": "Jane Smith"},
        "properties": [
            {"id": "prop-1", "name": "Villa Bougainvillea"},
        ],
    }
    mock_client.get_reservation_messages.return_value = []
    mock_client.get_property.return_value = {"description": "desc"}
    mock_client.get_property_knowledge_hub.return_value = {"topics": []}

    mock_classify.return_value = {
        "category": "URGENT_MAINTENANCE",
        "urgency": "HIGH",
        "summary": "AC broken",
    }
    mock_draft.return_value = "Draft response"
    mock_slack.return_value = {"ok": True}

    event = _make_webhook_event(SAMPLE_WEBHOOK_PAYLOAD)

    # First delivery — should process
    result1 = webhook_handler(event, None)
    assert json.loads(result1["body"])["status"] == "processed"
    assert mock_slack.call_count == 1

    # Second delivery — should deduplicate
    mock_slack.reset_mock()
    result2 = webhook_handler(event, None)
    assert json.loads(result2["body"])["status"] == "duplicate"
    mock_slack.assert_not_called()


@mock_aws
@patch("webhook_handler.get_webhook_secret", return_value="")
@patch("webhook_handler.HospitableClient")
@patch("webhook_handler.classify_message")
@patch("webhook_handler.draft_response")
@patch("webhook_handler.summarize_conversation", return_value="")
@patch("webhook_handler.post_guest_alert")
def test_webhook_error_does_not_mark_processed(
    mock_slack, mock_summary, mock_draft, mock_classify, mock_hospitable, mock_secret,
    dynamodb_table,
):
    """A transient error inside the pipeline must NOT mark the message as
    processed — the hourly safety-net poll needs to be able to retry it."""
    from state_tracker import StateTracker, message_dedup_key

    mock_client = MagicMock()
    mock_hospitable.return_value = mock_client
    # Simulate a transient Hospitable outage during enrichment.
    mock_client.get_reservation_detail.side_effect = RuntimeError("hospitable 500")

    event = _make_unsigned_webhook_event(SAMPLE_WEBHOOK_PAYLOAD)
    result = webhook_handler(event, None)

    # Return 200 so Hospitable doesn't retry on a transient error, but
    # content signals the failure.
    assert result["statusCode"] == 200
    body = json.loads(result["body"])
    assert body["status"] == "error"

    # Critically: the DynamoDB row must NOT exist, so the poll can retry.
    tracker = StateTracker()
    dedup_key = message_dedup_key(
        SAMPLE_WEBHOOK_PAYLOAD["data"]["reservation_id"],
        SAMPLE_WEBHOOK_PAYLOAD["data"],
    )
    assert tracker.is_message_processed(
        SAMPLE_WEBHOOK_PAYLOAD["data"]["reservation_id"], dedup_key
    ) is False


@mock_aws
@patch("webhook_handler.get_webhook_secret", return_value="")
@patch("webhook_handler.HospitableClient")
@patch("webhook_handler.classify_message")
@patch("webhook_handler.draft_response")
@patch("webhook_handler.summarize_conversation", return_value="")
@patch("webhook_handler.post_guest_alert")
@patch("handler.HospitableClient")
@patch("handler.classify_message")
@patch("handler.draft_response")
@patch("handler.summarize_conversation", return_value="")
@patch("handler.post_guest_alert")
def test_webhook_then_poller_posts_exactly_one_alert(
    mock_poll_slack,
    mock_poll_summary,
    mock_poll_draft,
    mock_poll_classify,
    mock_poll_hospitable,
    mock_slack,
    mock_summary,
    mock_draft,
    mock_classify,
    mock_hospitable,
    mock_secret,
    dynamodb_table,
):
    """Regression guard for the duplicate-alert bug:
    the webhook processes a message, then the hourly poller sees the SAME
    message in the API conversation history, and must dedupe to zero new
    alerts. Previously, the webhook keyed by webhook_event_id and the poller
    keyed by API message_id — different rows, so the poller re-alerted."""
    from handler import lambda_handler

    # ─── Webhook side ───
    mock_client = MagicMock()
    mock_hospitable.return_value = mock_client
    mock_client.get_reservation_detail.return_value = {
        "id": "res-uuid-123",
        "platform": "airbnb",
        "status": "accepted",
        "check_in": "2026-04-20T16:00:00-07:00",
        "check_out": "2026-04-25T11:00:00-07:00",
        "guest": {"first_name": "Jane", "full_name": "Jane Smith"},
        "properties": [{"id": "prop-1", "name": "Villa Bougainvillea"}],
    }
    mock_client.get_reservation_messages.return_value = []
    mock_client.get_property.return_value = {"description": "desc"}
    mock_client.get_property_knowledge_hub.return_value = {"topics": []}
    mock_classify.return_value = {"category": "URGENT", "urgency": "HIGH", "summary": "x"}
    mock_draft.return_value = "Draft"
    mock_slack.return_value = {"ok": True}

    event = _make_unsigned_webhook_event(SAMPLE_WEBHOOK_PAYLOAD)
    webhook_result = webhook_handler(event, None)
    assert json.loads(webhook_result["body"])["status"] == "processed"
    assert mock_slack.call_count == 1

    # ─── Poller side: same conversation, same message as an API object ───
    # The API form carries an `id` but the webhook form didn't. The shared
    # dedup key must still match.
    api_msg = {
        "id": "msg-abc-999",
        "body": SAMPLE_WEBHOOK_PAYLOAD["data"]["body"],
        "sender_type": "guest",
        "source": "platform",
        "created_at": SAMPLE_WEBHOOK_PAYLOAD["data"]["created_at"],
    }
    mock_poll_client = MagicMock()
    mock_poll_hospitable.return_value = mock_poll_client
    mock_poll_client.get_active_reservations.return_value = [
        {
            "id": "res-uuid-123",
            "property_id": "prop-1",
            "property_name": "Villa Bougainvillea",
            "check_in": "2026-04-20T16:00:00-07:00",
            "check_out": "2026-04-25T11:00:00-07:00",
            "status": "accepted",
            "platform": "airbnb",
            "guest": {"first_name": "Jane", "full_name": "Jane Smith"},
        }
    ]
    mock_poll_client.get_reservation_messages.return_value = [api_msg]
    mock_poll_client.get_property.return_value = {"description": "desc"}
    mock_poll_client.get_property_knowledge_hub.return_value = {"topics": []}

    poll_result = lambda_handler({}, None)
    poll_body = json.loads(poll_result["body"])

    # Zero new alerts from the poller — the webhook already handled it.
    assert poll_body["alerts_posted"] == 0
    assert poll_body["new_messages_found"] == 0
    mock_poll_slack.assert_not_called()


def test_verify_signature_valid():
    """Known secret + body should produce matching HMAC."""
    secret = "my-secret"
    body = '{"test": true}'
    expected = hmac.new(
        secret.encode("utf-8"),
        body.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    with patch("webhook_handler.get_webhook_secret", return_value=secret):
        assert verify_signature(body, expected) == "accepted"


def test_verify_signature_invalid():
    """Wrong signature should be rejected when secret is configured."""
    with patch("webhook_handler.get_webhook_secret", return_value="my-secret"):
        assert verify_signature('{"test": true}', "wrong-signature") == "rejected"


def test_verify_signature_no_secret_configured():
    """When no secret is configured, all requests should be accepted."""
    with patch("webhook_handler.get_webhook_secret", return_value=""):
        assert verify_signature('{"test": true}', "") == "accepted"
        assert verify_signature('{"test": true}', "anything") == "accepted"


def test_verify_signature_secret_but_no_header():
    """When secret is configured but no signature header, should reject."""
    with patch("webhook_handler.get_webhook_secret", return_value="my-secret"):
        assert verify_signature('{"test": true}', "") == "rejected"
