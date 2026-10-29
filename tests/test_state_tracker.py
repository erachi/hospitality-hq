"""Tests for DynamoDB state tracker."""

from moto import mock_aws
from state_tracker import StateTracker, message_dedup_key


@mock_aws
def test_mark_and_check_processed(dynamodb_table):
    """Message marked as processed should be detected on next check."""
    tracker = StateTracker()

    # Not processed yet
    assert tracker.is_message_processed("res-123", "msg-001") is False

    # Mark it
    tracker.mark_message_processed(
        "res-123", "msg-001", "URGENT_MAINTENANCE", "HIGH", "Villa Bougainvillea"
    )

    # Now it should be processed
    assert tracker.is_message_processed("res-123", "msg-001") is True


@mock_aws
def test_different_messages_tracked_independently(dynamodb_table):
    """Different messages on same reservation are tracked separately."""
    tracker = StateTracker()

    tracker.mark_message_processed(
        "res-123", "msg-001", "COMPLAINT", "MEDIUM", "The Palm Club"
    )

    assert tracker.is_message_processed("res-123", "msg-001") is True
    assert tracker.is_message_processed("res-123", "msg-002") is False


@mock_aws
def test_different_reservations_tracked_independently(dynamodb_table):
    """Same message ID on different reservations are tracked separately."""
    tracker = StateTracker()

    tracker.mark_message_processed(
        "res-123", "msg-001", "GENERAL", "LOW", "Villa Bougainvillea"
    )

    assert tracker.is_message_processed("res-123", "msg-001") is True
    assert tracker.is_message_processed("res-456", "msg-001") is False


@mock_aws
def test_get_reservation_history(dynamodb_table):
    """Should return all processed messages for a reservation."""
    tracker = StateTracker()

    tracker.mark_message_processed(
        "res-123", "msg-001", "COMPLAINT", "MEDIUM", "Villa Bougainvillea"
    )
    tracker.mark_message_processed(
        "res-123", "msg-002", "GENERAL", "LOW", "Villa Bougainvillea"
    )
    tracker.mark_message_processed(
        "res-456", "msg-003", "POSITIVE", "LOW", "The Palm Club"
    )

    history = tracker.get_reservation_history("res-123")
    assert len(history) == 2

    history_other = tracker.get_reservation_history("res-456")
    assert len(history_other) == 1


# ─── message_dedup_key ───────────────────────────────────────────────────


def test_dedup_key_matches_across_webhook_and_api_payloads():
    """The dedup key must be identical when derived from the webhook `data`
    object and from the matching API message object. Fixing this parity is
    the whole point of the shared key — without it, the hourly poller re-
    alerts every message the webhook already handled."""
    webhook_data = {
        "reservation_id": "res-uuid-123",
        "body": "The AC isn't working and it's really hot",
        "sender_type": "guest",
        "created_at": "2026-04-20T22:15:00Z",
    }
    # Same message as the Hospitable API returns it. Carries an `id`, uses a
    # tz offset instead of Z, and has extra fields — all should still produce
    # the same dedup key.
    api_message = {
        "id": "msg-999",
        "reservation_id": "res-uuid-123",
        "body": "The AC isn't working and it's really hot",
        "sender_type": "guest",
        "source": "platform",
        "created_at": "2026-04-20T22:15:00+00:00",
    }

    k1 = message_dedup_key("res-uuid-123", webhook_data)
    k2 = message_dedup_key("res-uuid-123", api_message)
    assert k1 == k2


def test_dedup_key_body_whitespace_insensitive():
    """Trailing whitespace in one payload but not the other must not break
    the match. Hospitable has been seen to trim differently on webhook vs API."""
    a = {"created_at": "2026-04-20T22:15:00Z", "body": "hi there"}
    b = {"created_at": "2026-04-20T22:15:00Z", "body": "  hi there\n"}
    assert message_dedup_key("r", a) == message_dedup_key("r", b)


def test_dedup_key_distinguishes_different_bodies():
    a = {"created_at": "2026-04-20T22:15:00Z", "body": "AC broken"}
    b = {"created_at": "2026-04-20T22:15:00Z", "body": "Pool is cold"}
    assert message_dedup_key("r", a) != message_dedup_key("r", b)


def test_dedup_key_distinguishes_different_times():
    a = {"created_at": "2026-04-20T22:15:00Z", "body": "same"}
    b = {"created_at": "2026-04-20T22:16:00Z", "body": "same"}
    assert message_dedup_key("r", a) != message_dedup_key("r", b)


def test_dedup_key_handles_null_body():
    """Attachment-only messages have body=None in the API. Must not crash."""
    k = message_dedup_key("r", {"created_at": "2026-04-20T22:15:00Z", "body": None})
    assert k.startswith("ts:")
