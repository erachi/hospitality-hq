"""Tests for DynamoDB state tracker."""

from moto import mock_aws
from state_tracker import StateTracker


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
